#!/usr/bin/env python3
"""GA Realtime bridge diagnostic (LANDING-25.0.3 Part D).

Run INSIDE the backend container, where OPENAI_API_KEY + network exist:

    docker exec emma-backend python3 /app/scripts_diag_realtime.py

Connects to OpenAI exactly like the demo bridge (no beta header), sends the
GA session.update, feeds 200ms of synthetic silence, and prints the event types
that come back. PASS = session.created + session.updated + a response.* /
output_audio event with NO 4000 close.
"""
from __future__ import annotations

import asyncio
import json
import sys

import websockets

sys.path.insert(0, "/app")
from backend.config import settings
from backend.demo_session import session_config


async def main() -> int:
    url = f"{settings.OPENAI_REALTIME_URL}?model={settings.OPENAI_REALTIME_MODEL}"
    headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}  # NO OpenAI-Beta
    seen: list[str] = []
    try:
        async with websockets.connect(url, additional_headers=headers, max_size=None) as oai:
            # 1) await session.created
            while True:
                ev = json.loads(await asyncio.wait_for(oai.recv(), 10))
                seen.append(ev["type"])
                if ev["type"] == "session.created":
                    print("✓ session.created  model=", ev.get("session", {}).get("model"))
                    break
                if ev["type"] == "error":
                    print("✗ error before session.created:", ev.get("error"))
                    return 1
            # 2) send GA session.update
            await oai.send(json.dumps(session_config("es")))
            # 3) feed 200ms of 24kHz PCM16 silence, then ask for a reply
            import base64
            silence = base64.b64encode(b"\x00\x00" * 4800).decode()
            await oai.send(json.dumps({"type": "input_audio_buffer.append", "audio": silence}))
            await oai.send(json.dumps({"type": "response.create"}))
            # 4) collect events for a few seconds
            updated = audio = False
            try:
                async with asyncio.timeout(8):
                    async for raw in oai:
                        ev = json.loads(raw)
                        t = ev["type"]
                        seen.append(t)
                        if t == "session.updated":
                            updated = True
                            print("✓ session.updated")
                        if t in ("response.output_audio.delta", "response.audio.delta"):
                            audio = True
                            print("✓ audio delta flowing:", t)
                            break
                        if t == "error":
                            print("✗ error:", ev.get("error"))
                            return 1
            except TimeoutError:
                pass
            print("\nevent types seen:", sorted(set(seen)))
            ok = updated and audio
            print("\nRESULT:", "PASS — GA bridge healthy" if ok else "PARTIAL — check the field OpenAI named")
            return 0 if ok else 1
    except websockets.exceptions.ConnectionClosed as e:
        print(f"✗ WS closed code={e.code} reason={e.reason!r}")
        if e.code == 4000:
            print("  → still beta shape somewhere; compare session.update vs the error.")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
