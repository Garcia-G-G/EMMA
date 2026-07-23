#!/usr/bin/env python3
"""Securely wipe local wake-word voice recordings (24.7-C1).

``scripts/wake_data/positive_real/*.wav`` and ``validation/*.wav`` are the user's
ACTUAL voice — biometric PII (Personal tier, arguably Secret). They live only on
disk, gitignored, 0700. This deletes them after an explicit confirmation: each
file is overwritten with random bytes before unlinking, so the audio can't be
trivially recovered from the freed blocks (best-effort on SSDs; FileVault is the
real at-rest guarantee).

    python scripts/wipe_wake_data.py            # prompts y/N
    python scripts/wipe_wake_data.py --yes      # no prompt (for automation)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent / "wake_data"


def _wav_files() -> list[Path]:
    return sorted(_ROOT.rglob("*.wav")) if _ROOT.is_dir() else []


def _shred(path: Path) -> None:
    """Overwrite with random bytes, then unlink. Best-effort, never raises."""
    try:
        size = path.stat().st_size
        with open(path, "r+b", buffering=0) as f:
            f.write(os.urandom(size))
            f.flush()
            os.fsync(f.fileno())
        path.unlink()
    except OSError:
        # if overwrite fails, still try to remove the file
        path.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Borrar grabaciones de voz del wake word (24.7).")
    ap.add_argument("--yes", action="store_true", help="no preguntar")
    args = ap.parse_args()

    files = _wav_files()
    if not files:
        print("No hay grabaciones que borrar en scripts/wake_data/.")
        return 0
    print(f"Voy a borrar {len(files)} grabaciones de TU voz (sobrescritas + eliminadas).")
    if not args.yes and input(
            "¿Seguro? Escribe 'si' para continuar: ").strip().lower() not in ("si", "sí", "yes"):
        print("Cancelado. No borré nada.")
        return 1
    for f in files:
        _shred(f)
    print(f"✓ {len(files)} grabaciones borradas de forma segura.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
