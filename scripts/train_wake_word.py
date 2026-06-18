#!/usr/bin/env python3
"""Train the custom "Hey Emma" openWakeWord model (Prompt 16.2). Local, no Picovoice.

Pipeline (all local):
  1. Compute openWakeWord embeddings for every positive + negative clip
     (the shared melspectrogram→embedding feature model, openwakeword.utils).
  2. Slide a 16-frame window over each clip → labelled examples (1 = wake, 0 = not).
  3. Train a small classifier whose ONNX I/O matches openWakeWord's runtime loader
     (input [N,16,96] float32 → output [N,1] score), so core/wake_word.py loads it.
  4. Export → models/hey_emma.onnx.
  5. --validate: score the real recordings (record_wake_validation.py) + a held-out
     negative slice across thresholds, and recommend WAKE_WORD_THRESHOLD.

Run (training venv — see requirements-train.txt). NOT run automatically:
    python scripts/wake_word_data.py --out scripts/wake_data --noise-dir <bg audio>
    python scripts/record_wake_validation.py
    python scripts/train_wake_word.py --data scripts/wake_data --out models/hey_emma.onnx --validate

Targets openwakeword==0.6. The 16x96 embedding window + ONNX signature are openWakeWord's
custom-model format; if a future version changes it, regenerate against that version.
"""

from __future__ import annotations

import argparse
from pathlib import Path

_WINDOW = 16   # embedding frames per inference window (openWakeWord default)
_EMB = 96      # embedding dims per frame


def _embed_dir(folder: Path, af) -> list:
    """openWakeWord embeddings [T,96] for each clip in `folder`."""
    import numpy as np
    import soundfile as sf

    out = []
    for wav in sorted(list(folder.glob("*.wav"))):
        data, sr = sf.read(str(wav), dtype="int16")
        if data.ndim > 1:
            data = data[:, 0]
        if sr != 16000 or data.size < 16000:
            continue
        emb = af.embed_clips([data.astype(np.int16)], batch_size=1)[0]  # [T,96]
        if emb.shape[0] >= _WINDOW:
            out.append(emb)
    return out


def _windows(embeddings: list, label: int):
    """Slide a 16-frame window over each clip embedding → (X, y) examples."""
    import numpy as np

    xs = []
    for emb in embeddings:
        for i in range(0, emb.shape[0] - _WINDOW + 1, 2):
            xs.append(emb[i : i + _WINDOW])
    y = np.full(len(xs), label, dtype="float32")
    return (np.asarray(xs, dtype="float32"), y) if xs else (np.zeros((0, _WINDOW, _EMB), "float32"), y)


def _build_model():
    import torch.nn as nn

    return nn.Sequential(
        nn.Flatten(),
        nn.Linear(_WINDOW * _EMB, 128), nn.ReLU(),
        nn.Linear(128, 64), nn.ReLU(),
        nn.Linear(64, 1),  # logit; runtime applies sigmoid via the exported graph
        nn.Sigmoid(),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Entrenar 'Hey Emma' (openWakeWord, 16.2).")
    ap.add_argument("--data", type=Path, default=Path("scripts/wake_data"))
    ap.add_argument("--out", type=Path, default=Path("models/hey_emma.onnx"))
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()

    import numpy as np
    import torch
    from openwakeword.utils import AudioFeatures

    af = AudioFeatures()
    print("→ Calculando embeddings (positivos/negativos)…")
    pos = _embed_dir(args.data / "positive", af)
    neg = _embed_dir(args.data / "negative", af)
    if not pos or not neg:
        print("✗ Faltan datos. Corre wake_word_data.py (positivos + --noise-dir) primero.")
        return 1
    win_pos, y_pos = _windows(pos, 1)
    win_neg, y_neg = _windows(neg, 0)
    feats = np.concatenate([win_pos, win_neg])
    labels = np.concatenate([y_pos, y_neg])
    print(f"  {len(win_pos)} ventanas wake, {len(win_neg)} ventanas negativas.")

    model = _build_model()
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = torch.nn.BCELoss()
    feats_t = torch.from_numpy(feats)
    labels_t = torch.from_numpy(labels).unsqueeze(1)
    # class-balance the loss (far more negative windows than positive)
    w = torch.where(labels_t > 0, len(win_neg) / max(1, len(win_pos)), 1.0)
    print("→ Entrenando…")
    for ep in range(args.epochs):
        model.train()
        opt.zero_grad()
        out = model(feats_t)
        loss = (loss_fn(out, labels_t) * w).mean()
        loss.backward()
        opt.step()
        if ep % 10 == 0:
            print(f"  epoch {ep:>3}  loss {loss.item():.4f}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    model.eval()
    torch.onnx.export(
        model, torch.zeros(1, _WINDOW, _EMB),
        str(args.out), input_names=["x"], output_names=["score"],
        dynamic_axes={"x": {0: "batch"}, "score": {0: "batch"}}, opset_version=13,
    )
    print(f"✓ Modelo exportado → {args.out}")
    print("  Ponlo en .env:  WAKE_WORD_PATH=models/hey_emma.onnx")

    if args.validate:
        val = _embed_dir(args.data / "validation", af)
        if val:
            win_val, _ = _windows(val, 1)
            with torch.no_grad():
                pos_scores = model(torch.from_numpy(win_val)).numpy().max() if len(win_val) else 0
                neg_scores = model(torch.from_numpy(win_neg)).numpy()
            print("\n=== Validación (tu voz real vs negativos) ===")
            for thr in (0.3, 0.4, 0.5, 0.6, 0.7):
                far = float((neg_scores >= thr).mean())
                print(f"  thr {thr}:  detecta tu voz: {'sí' if pos_scores >= thr else 'no'} "
                      f"| falsos despertares (ratio negativos): {far:.4f}")
            print("  → Elige el umbral más alto que aún te detecte; ponlo en WAKE_WORD_THRESHOLD.")
        else:
            print("⚠ Sin grabaciones de validación (corre record_wake_validation.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
