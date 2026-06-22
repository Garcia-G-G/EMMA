#!/usr/bin/env python3
"""Train the custom "Hey Emma" openWakeWord model (Prompt 16.2). Local, no Picovoice.

Pipeline (all local):
  1. Compute openWakeWord embeddings for every positive + negative clip
     (the shared melspectrogram->embedding feature model, openwakeword.utils).
  2. Slide a 16-frame window over each clip -> labelled examples (1 = wake, 0 = not).
  3. Train a small classifier whose ONNX I/O matches openWakeWord's runtime loader
     (input [N,16,96] float32 -> output [N,1] score), so core/wake_word.py loads it.
  4. Export -> models/hey_emma.onnx.
  5. --validate: score the real recordings (record_wake_validation.py) + a held-out
     negative slice across thresholds, and recommend WAKE_WORD_THRESHOLD.

Run (training venv - see requirements-train.txt). NOT run automatically:
    python scripts/wake_word_data.py --out scripts/wake_data --noise-dir <bg audio>
    python scripts/record_wake_validation.py
    python scripts/train_wake_word.py --data scripts/wake_data --out models/hey_emma.onnx --validate

Targets openwakeword==0.6. The 16x96 embedding window + ONNX signature are openWakeWord's
custom-model format; if a future version changes it, regenerate against that version.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

_WINDOW = 16        # embedding frames per inference window (openWakeWord default)
_EMB = 96           # embedding dims per frame
_TARGET_LEN = 40000  # pad/truncate every clip to 2.5 s at 16 kHz (see _embed_dir)
_TARGET_SR = 16000


def _embed_dir(folder: Path, af) -> list:
    """openWakeWord embeddings [T,96] for each clip in `folder`."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    # Pad/truncate every clip to TARGET_LEN samples. openWakeWord's
    # classifier expects 16-frame sliding windows at 80 ms hop on top
    # of a 760 ms feature context, so audio MUST be ≥ ~2.04 s to yield
    # 16 embedding frames. 2.5 s gives headroom + the pad/truncate also
    # normalizes the melspec pre-allocation buffer (which off-by-ones
    # on variable-length input).
    out = []
    for wav in sorted(list(folder.glob("*.wav"))):
        data, sr = sf.read(str(wav), dtype="int16")
        if data.ndim > 1:
            data = data[:, 0]
        # Resample to 16 kHz if needed (ElevenLabs returns 22050 or 44100 Hz)
        if sr != _TARGET_SR:
            data = resample_poly(data.astype(np.float32), _TARGET_SR, sr).astype(np.int16)
        if data.size < 8000:  # < 0.5 s after resample -> too short, skip
            continue
        # Normalize length
        if data.size < _TARGET_LEN:
            data = np.pad(data, (0, _TARGET_LEN - data.size))
        elif data.size > _TARGET_LEN:
            data = data[:_TARGET_LEN]
        emb = af.embed_clips(data.astype(np.int16)[np.newaxis, :], batch_size=1)[0]  # [T,96]
        if emb.shape[0] >= _WINDOW:
            out.append(emb)
    return out


# Minimum frame-to-frame variation for a window to count as a training example.
# Short clips get zero-padded to reach 16 frames; sliding windows that land mostly
# on that padding are nearly-constant (silence embeddings repeat). Labeling those
# windows positive taught the model "silence -> wake" (it scored pure silence ~1.0).
# Dropping low-variation windows removes the leakage. Measured: pure silence ~ 0.0,
# padding-dominated positive windows ~ 0.1-0.5, real-speech windows ~ 1.5-5.
_MIN_WINDOW_VAR = 1.0


def _windows(embeddings: list, label: int, drop_padding: bool = False):
    """Slide a 16-frame window over each clip embedding -> (X, y) examples.

    With ``drop_padding`` (use for POSITIVES), windows dominated by silence/zero
    padding are skipped so they can't teach "silence ⇒ wake". Negatives keep
    those windows on purpose - silence MUST appear as a negative so the model
    learns silence ⇒ not-wake.
    """
    import numpy as np

    xs = []
    for emb in embeddings:
        for i in range(0, emb.shape[0] - _WINDOW + 1, 2):
            w = emb[i : i + _WINDOW]
            if drop_padding and float(np.abs(np.diff(w, axis=0)).mean()) < _MIN_WINDOW_VAR:
                continue  # mostly padding - don't label this as the wake word
            xs.append(w)
    y = np.full(len(xs), label, dtype="float32")
    return (np.asarray(xs, dtype="float32"), y) if xs else (np.zeros((0, _WINDOW, _EMB), "float32"), y)


def _silence_negative_windows(af) -> Any:
    """Explicit silence + low-noise NEGATIVE windows.

    A model trained only on speech windows scores silence arbitrarily (we saw
    ~1.0). Feeding pure silence and a range of room-tone noise amplitudes as
    negatives anchors silence/quiet ⇒ not-wake.
    """
    import numpy as np

    out = []
    rng = np.random.default_rng(0)
    clips = [np.zeros(_TARGET_LEN, dtype=np.int16)]
    clips += [(rng.standard_normal(_TARGET_LEN) * amp).astype(np.int16) for amp in (20, 60, 150, 400)]
    for x in clips:
        emb = af.embed_clips(x[np.newaxis, :], batch_size=1)[0]
        for i in range(0, emb.shape[0] - _WINDOW + 1, 2):
            out.append(emb[i : i + _WINDOW])
    return np.asarray(out, dtype="float32")


def _build_model():
    import torch.nn as nn

    return nn.Sequential(
        nn.Flatten(),
        nn.Linear(_WINDOW * _EMB, 128), nn.ReLU(),
        nn.Linear(128, 64), nn.ReLU(),
        nn.Linear(64, 1),  # logit; runtime applies sigmoid via the exported graph
        nn.Sigmoid(),
    )


_THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7)


class TrainingError(RuntimeError):
    """Training could not produce a usable model (no data, or divergence)."""


def _clip_max_scores(model, torch, clips: list) -> list:
    """Per-clip max wake-window score - how a clip would score at inference."""
    out = []
    for emb in clips:
        win, _ = _windows([emb], 1)
        if win.shape[0] == 0:
            continue
        with torch.no_grad():
            out.append(float(model(torch.from_numpy(win)).numpy().max()))
    return out


def train(data: Path, out: Path, epochs: int = 40, validate: bool = True,
          on_progress: Callable[[str, int, int, str], None] | None = None) -> dict:
    """Train the wake model and export ONNX. Returns a stats dict.

    on_progress(phase, done, total, message) fires through the run: phase is one
    of "embedding"/"training"/"validating". Raises TrainingError on missing data
    or divergence (NaN loss) so the caller can surface an honest failure.

    The heavy deps (numpy/torch/openwakeword) import lazily here so importing
    this module costs nothing at the daemon/backend level.
    """
    import numpy as np
    import torch
    from openwakeword.utils import AudioFeatures

    def _emit(phase: str, done: int, total: int, msg: str) -> None:
        if on_progress:
            on_progress(phase, done, total, msg)

    af = AudioFeatures()
    _emit("embedding", 0, 1, "Calculando embeddings…")
    pos = _embed_dir(data / "positive", af)
    # Real recordings of the user saying the wake word matter most for recognizing
    # THEIR voice (synthetic TTS alone generalizes poorly across accents) - include
    # them as positives whenever the optional positive_real/ directory exists.
    real_dir = data / "positive_real"
    if real_dir.is_dir():
        real = _embed_dir(real_dir, af)
        pos = pos + real
        _emit("embedding", 0, 1, f"{len(real)} positivos de tu voz incluidos")
    neg = _embed_dir(data / "negative", af)
    if not pos or not neg:
        raise TrainingError("Faltan datos de entrenamiento (positivos o negativos).")
    # Positives drop padding-dominated windows (no "silence ⇒ wake" leak);
    # negatives keep theirs AND we add explicit silence/noise so the model
    # firmly learns silence ⇒ not-wake.
    win_pos, y_pos = _windows(pos, 1, drop_padding=True)
    win_neg, y_neg = _windows(neg, 0, drop_padding=False)
    quiet = _silence_negative_windows(af)
    feats = np.concatenate([win_pos, win_neg, quiet])
    labels = np.concatenate([y_pos, y_neg, np.zeros(len(quiet), dtype="float32")])
    win_neg_total = len(win_neg) + len(quiet)
    _emit("embedding", 1, 1, f"{len(win_pos)} ventanas wake, {win_neg_total} negativas")

    model = _build_model()
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = torch.nn.BCELoss()
    feats_t = torch.from_numpy(feats)
    labels_t = torch.from_numpy(labels).unsqueeze(1)
    w = torch.where(labels_t > 0, win_neg_total / max(1, len(win_pos)), 1.0)
    for ep in range(epochs):
        model.train()
        opt.zero_grad()
        pred = model(feats_t)
        loss = (loss_fn(pred, labels_t) * w).mean()
        if torch.isnan(loss):
            raise TrainingError("El entrenamiento no convergió. Intenta con más muestras o frases distintas.")
        loss.backward()
        opt.step()
        _emit("training", ep + 1, epochs, f"Epoch {ep + 1}/{epochs}")

    out.parent.mkdir(parents=True, exist_ok=True)
    model.eval()
    # Force the legacy exporter (dynamo=False) so the model + weights ship
    # as a single .onnx file. The new dynamo path splits weights into a
    # separate .onnx.data sidecar, which the runtime loader doesn't expect.
    torch.onnx.export(
        model, torch.zeros(1, _WINDOW, _EMB),
        str(out), input_names=["x"], output_names=["score"],
        dynamic_axes={"x": {0: "batch"}, "score": {0: "batch"}}, opset_version=13,
        dynamo=False,
    )

    stats: dict = {
        "positive_windows": int(win_pos.shape[0]),
        "negative_windows": int(win_neg.shape[0]),
        "model_path": str(out),
        "recommended_threshold": 0.5,
        "validation": None,
    }
    if validate:
        _emit("validating", 0, 1, "Validando contra tus grabaciones…")
        val = _embed_dir(data / "validation", af)
        pos_scores = _clip_max_scores(model, torch, val)
        neg_scores = _clip_max_scores(model, torch, neg)
        table = []
        for thr in _THRESHOLDS:
            detected = sum(1 for s in pos_scores if s >= thr)
            false_wakes = sum(1 for s in neg_scores if s >= thr)
            table.append({"threshold": thr, "detected": detected, "false_wakes": false_wakes})
        # recommend the highest threshold that still catches ≥60% of real clips.
        rec = 0.5
        if pos_scores:
            for row in table:
                if row["detected"] / len(pos_scores) >= 0.6:
                    rec = row["threshold"]
        stats["recommended_threshold"] = rec
        stats["validation"] = {
            "positives_total": len(pos_scores),
            "negatives_total": len(neg_scores),
            "thresholds": table,
        }
        _emit("validating", 1, 1, f"Umbral recomendado: {rec}")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Entrenar 'Hey Emma' (openWakeWord, 16.2).")
    ap.add_argument("--data", type=Path, default=Path("scripts/wake_data"))
    ap.add_argument("--out", type=Path, default=Path("models/hey_emma.onnx"))
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()

    def _print(phase: str, done: int, total: int, msg: str) -> None:
        print(f"  [{phase}] {msg}")

    try:
        stats = train(args.data, args.out, args.epochs, args.validate, on_progress=_print)
    except TrainingError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"✓ Modelo exportado -> {stats['model_path']}")
    val = stats.get("validation")
    if val:
        print(f"\n=== Validación: {val['positives_total']} clips reales vs "
              f"{val['negatives_total']} negativos ===")
        for row in val["thresholds"]:
            print(f"  thr {row['threshold']}:  detecta {row['detected']}/{val['positives_total']} "
                  f"| falsos despertares {row['false_wakes']}/{val['negatives_total']}")
        print(f"  -> Umbral recomendado: WAKE_WORD_THRESHOLD={stats['recommended_threshold']}")
    elif args.validate:
        print("⚠ Sin grabaciones de validación (corre record_wake_validation.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
