"""Drive the real cascade with synthesized speech instead of a microphone (VOICE-1).

    python -m scripts.voice.probe "Emma, abre Safari" "Hey Emma, ¿cómo estás?"

Each utterance is synthesized with the configured voice at 16 kHz, streamed in
real time into the SAME stages ``core.cascade`` builds (VAD -> Scribe -> taps ->
BrainProcessor -> TTS), followed by silence so the VAD closes the turn. Prints,
per utterance: the transcript, the brain's verdict, what Emma said, and the
latency from the end of speech (VAD stop) to her first audio byte.

Live services, real money (a few cents). No speaker, no mic, no tools run unless
``--execute`` is on (then they run for real, through dispatch and its guard).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import aiohttp
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.elevenlabs.tts import ElevenLabsHttpTTSService

from config.settings import settings
from core import brain_shadow, cascade

CHUNK_MS = 20


async def synth(http: aiohttp.ClientSession, text: str) -> bytes:
    async with http.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{settings.VOICE_ID}",
        params={"output_format": "pcm_16000"},
        headers={"xi-api-key": settings.ELEVENLABS_API_KEY},
        json={"text": text, "model_id": settings.ELEVENLABS_TTS_MODEL},
    ) as r:
        r.raise_for_status()
        return await r.read()


class Source(FrameProcessor):
    def __init__(self, clips: list[bytes]) -> None:
        super().__init__()
        self._clips = clips
        self.done = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.create_task(self._feed())

    async def _feed(self) -> None:
        step = cascade.MIC_RATE_HZ * 2 * CHUNK_MS // 1000
        silence = b"\0" * step
        for clip in self._clips:
            for i in range(0, len(clip), step):
                await self.push_frame(InputAudioRawFrame(clip[i:i + step].ljust(step, b"\0"),
                                                         cascade.MIC_RATE_HZ, 1))
                await asyncio.sleep(CHUNK_MS / 1000)
            for _ in range(int(4000 / CHUNK_MS)):  # 4 s: VAD stop + Emma's answer
                await self.push_frame(InputAudioRawFrame(silence, cascade.MIC_RATE_HZ, 1))
                await asyncio.sleep(CHUNK_MS / 1000)
        self.done.set()


class Marks:
    """Per-turn timings, filled by three taps placed around the stages."""

    def __init__(self) -> None:
        self.turns: list[dict[str, object]] = []
        self.stop = 0.0

    @property
    def cur(self) -> dict[str, object] | None:
        return self.turns[-1] if self.turns else None


class Tap(FrameProcessor):
    def __init__(self, marks: Marks) -> None:
        super().__init__()
        self.m = marks

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        m, cur, now = self.m, self.m.cur, time.monotonic()
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            m.stop = now
        elif isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            if cur is None or cur.get("heard") != frame.text:
                m.turns.append({"heard": frame.text,
                                "stop_to_transcript_ms": round((now - m.stop) * 1000)})
        elif isinstance(frame, TTSSpeakFrame) and cur is not None:
            cur["said"] = frame.text
        elif isinstance(frame, TTSAudioRawFrame) and cur is not None and "stop_to_audio_ms" not in cur:
            cur["stop_to_audio_ms"] = round((now - m.stop) * 1000)
        await self.push_frame(frame, direction)


async def main(texts: list[str], execute: bool = False) -> None:
    if brain_shadow.loaded() is None:
        await asyncio.to_thread(brain_shadow._load_brain)
    async with aiohttp.ClientSession() as http:
        clips = [await synth(http, t) for t in texts]
        source = Source(clips)
        marks = Marks()
        records: list[dict[str, object]] = []
        turns = cascade._Turns()

        def record(d, said, extra):  # type: ignore[no-untyped-def]
            records.append({"verdict": d.verdict, "reason": d.reason, "tool": d.chosen, **extra})
            turns.record(d, said, {**extra, "probe": True})

        from core.brain.processor import BrainProcessor

        stt = cascade.ScribeSTT(
            api_key=settings.ELEVENLABS_API_KEY, sample_rate=cascade.MIC_RATE_HZ,
            include_language_detection=True, audio_passthrough=False,
            settings=cascade.ScribeSTT.Settings(model=settings.ELEVENLABS_STT_MODEL),
        )
        tts = cascade.CachedTTS(
            api_key=settings.ELEVENLABS_API_KEY, aiohttp_session=http,
            sample_rate=cascade.SPEAKER_RATE_HZ,
            settings=ElevenLabsHttpTTSService.Settings(voice=settings.VOICE_ID,
                                                       model=settings.ELEVENLABS_TTS_MODEL),
            cacheable=cascade.cacheable_lines(),
        )
        vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(
            sample_rate=cascade.MIC_RATE_HZ, params=VADParams(stop_secs=settings.CASCADE_VAD_STOP_S)))
        pipeline = Pipeline([source, vad, Tap(marks), stt, cascade._UserTurnTap(),
                             BrainProcessor(cascade._decide_getter, record=record,
                                            host=cascade._Host() if execute else None),
                             Tap(marks), tts, Tap(marks)])
        task = PipelineTask(pipeline, params=PipelineParams(
            audio_in_sample_rate=cascade.MIC_RATE_HZ, audio_out_sample_rate=cascade.SPEAKER_RATE_HZ))

        async def stop_when_done() -> None:
            await source.done.wait()
            await task.queue_frame(EndFrame())

        asyncio.get_running_loop().create_task(stop_when_done())
        await PipelineRunner(handle_sigint=False).run(task)
        for t, r in zip(marks.turns, records, strict=False):
            print(json.dumps({**t, **{k: r[k] for k in ("verdict", "reason", "tool", "path", "latency")
                                      if k in r}}, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("texts", nargs="+")
    ap.add_argument("--execute", action="store_true", help="really run the tools (Part 2)")
    a = ap.parse_args()
    asyncio.run(main(a.texts, a.execute))
