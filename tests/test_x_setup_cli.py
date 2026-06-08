"""Prompt 26.2: `emma.x_setup` is now a thin alias for `emma.setup --only x`.

(The X flow itself moved into core.x_oauth.run_pkce_setup — see test_x_oauth.py.)
"""

from __future__ import annotations

from emma import x_setup


def test_x_setup_delegates_to_unified_setup(monkeypatch):
    captured: dict = {}

    def _fake(argv=None):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(x_setup, "_setup_main", _fake)
    assert x_setup.main() == 0
    assert captured["argv"] == ["--only", "x"]
