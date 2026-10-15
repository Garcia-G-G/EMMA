"""Which landing scenes the local brain serves today (Step 40.2).

    python scripts/landing_scenes.py

Visible scenes must pass (tests/test_landing_scenes.py); a hidden scene that now
passes in both languages can lose its `hidden` attribute.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.brain.brain import Brain
from core.brain.slots import Vocab
from core.brain_host import build_index, thresholds_from_settings, tool_docs, vocab
from tests.brain_slot_fixtures import APPS, CONNECTIONS, NOTES, PAGES, PLAYLISTS
from tests.test_landing_scenes import EMBEDDER, LANDING, scenes


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        index = build_index(EMBEDDER, Path(tmp) / "brain.db")
        base = vocab(apps=APPS, notes=NOTES, playlists=PLAYLISTS, connections=CONNECTIONS)
        v = Vocab(apps=base.apps, aliases=base.aliases, pages=PAGES, notes=base.notes,
                  playlists=base.playlists, connections=base.connections, contacts=base.contacts)
        brain = Brain(tool_docs(), v, thresholds_from_settings(), index, EMBEDDER)
        bad_visible = 0
        for s in scenes(LANDING.read_text(encoding="utf-8")):
            ok = all(
                (d := brain.decide(str(s[k]))).verdict == "local" and d.chosen == s["data-tool"]
                for k in ("data-utterance", "data-utterance-en")
            )
            state = "hidden " if s["hidden"] else "visible"
            print(f"{state}  {'PASS' if ok else 'fail'}  {s['data-tool']:18} {s['data-utterance']}")
            bad_visible += (not s["hidden"] and not ok)
        index.close()
    return 1 if bad_visible else 0


if __name__ == "__main__":
    raise SystemExit(main())
