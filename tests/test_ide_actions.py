"""Phase 19: IDE actions — CLI selection + open-at-line argv."""

from __future__ import annotations

import pytest

from tools import ide_actions


class TestOpenArgs:
    def test_vscode_cursor_use_goto(self):
        assert ide_actions._open_args("/x/code", "VS Code", "/p/f.py", 314) == [
            "/x/code",
            "-g",
            "/p/f.py:314",
        ]
        assert ide_actions._open_args("/x/cursor", "Cursor", "/p/f.py", 5) == [
            "/x/cursor",
            "-g",
            "/p/f.py:5",
        ]

    def test_zed_uses_bare_file_line(self):
        assert ide_actions._open_args("/x/zed", "Zed", "/p/f.py", 10) == ["/x/zed", "/p/f.py:10"]

    def test_no_line_just_path(self):
        assert ide_actions._open_args("/x/code", "VS Code", "/p/f.py", 0) == ["/x/code", "/p/f.py"]

    def test_no_cli_falls_back_to_open(self):
        assert ide_actions._open_args(None, "Cursor", "/p/f.py", 0) == [
            "open",
            "-a",
            "Cursor",
            "/p/f.py",
        ]


class TestCliFor:
    def test_maps_display_name_to_binary(self, monkeypatch):
        monkeypatch.setattr(
            ide_actions.shutil,
            "which",
            lambda b: f"/usr/bin/{b}" if b in ("code", "cursor", "zed") else None,
        )
        assert ide_actions._cli_for("Cursor") == "/usr/bin/cursor"
        assert ide_actions._cli_for("Visual Studio Code") == "/usr/bin/code"
        assert ide_actions._cli_for("Sublime Text") is None  # no CLI mapping


class TestOpenInIde:
    @pytest.mark.asyncio
    async def test_open_at_line_invokes_goto(self, monkeypatch, tmp_path):
        f = tmp_path / "x.py"
        f.write_text("hi")
        captured = {}

        class _FakeProc:
            async def wait(self):
                return 0

        async def _fake_exec(*args, **kwargs):
            captured["args"] = args
            return _FakeProc()

        monkeypatch.setattr(ide_actions, "resolve", lambda c: "Cursor")
        monkeypatch.setattr(ide_actions, "_cli_for", lambda app: "/usr/local/bin/cursor")
        monkeypatch.setattr(ide_actions.asyncio, "create_subprocess_exec", _fake_exec)

        r = await ide_actions.open_in_ide(str(f), line=314)
        assert r.success is True
        assert captured["args"] == ("/usr/local/bin/cursor", "-g", f"{f}:314")

    @pytest.mark.asyncio
    async def test_missing_file_errors(self, monkeypatch):
        monkeypatch.setattr(ide_actions, "resolve", lambda c: "Cursor")
        r = await ide_actions.open_in_ide("/nonexistent/zzz12345.py")
        assert r.success is False

    @pytest.mark.asyncio
    async def test_no_ide_configured(self, monkeypatch):
        monkeypatch.setattr(ide_actions, "resolve", lambda c: None)
        r = await ide_actions.open_in_ide("/tmp/whatever.py")
        assert r.success is False
        assert "IDE" in r.user_message
