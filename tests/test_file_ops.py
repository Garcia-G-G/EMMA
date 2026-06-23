"""Prompt 36 — file operations (mdfind/du mocked; rename on real tmp files)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

import tools.file_ops_tool as fo

# ---- parsers / mapping ------------------------------------------------------


def test_since_parser() -> None:
    assert fo._since_start("") is None
    assert fo._since_start("ayer").date() == dt.date.today() - dt.timedelta(days=1)
    assert fo._since_start("esta semana").date() == dt.date.today() - dt.timedelta(days=7)
    assert fo._since_start("este mes").day == 1
    assert fo._since_start("diciembre").month == 12
    assert fo._since_start("ksjdfh") is None


def test_kind_mapping_in_expr() -> None:
    e = fo._build_mdfind_expr("contrato", "pdf", "")
    assert "com.adobe.pdf" in e and "contrato" in e
    assert fo._build_mdfind_expr("", "image", "") == 'kMDItemContentTypeTree == "public.image"'
    assert "$time.iso" in fo._build_mdfind_expr("x", "", "diciembre")


# ---- A: find_file -----------------------------------------------------------


@pytest.mark.asyncio
async def test_find_file_maps_kind_and_summarizes(monkeypatch, tmp_path) -> None:
    f1 = tmp_path / "contrato dic.pdf"
    f1.write_text("a")
    f2 = tmp_path / "otro.pdf"
    f2.write_text("b")

    async def fake_run(args, timeout=15.0):
        assert "com.adobe.pdf" in args[-1]  # kind mapped into the mdfind expr
        return (0, f"{f1}\n{f2}\n")

    monkeypatch.setattr(fo, "_run", fake_run)
    res = await fo.find_file("contrato", kind="pdf", since="diciembre")
    assert res.success and res.data["count"] == 2
    assert "PDFs" in res.user_message


@pytest.mark.asyncio
async def test_find_file_empty(monkeypatch) -> None:
    monkeypatch.setattr(fo, "_run", lambda *a, **k: _async((0, "")))
    res = await fo.find_file("nada")
    assert res.success and res.data["results"] == []


# ---- B: analyze_disk_usage --------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_disk_usage_orders_desc(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(fo, "_resolve_in_home", lambda raw: tmp_path)
    du_out = f"100\t{tmp_path}/small\n5000\t{tmp_path}/big\n50\t{tmp_path}\n"
    monkeypatch.setattr(fo, "_run", lambda *a, **k: _async((0, du_out)))
    res = await fo.analyze_disk_usage(str(tmp_path))
    assert res.success
    names = [e["name"] for e in res.data["entries"]]
    assert names == ["big", "small"]  # largest first, base row skipped


# ---- C: free_space_assist ---------------------------------------------------


@pytest.mark.asyncio
async def test_free_space_preview_then_trash(monkeypatch, tmp_path) -> None:
    dmg = tmp_path / "old.dmg"
    dmg.write_text("x" * 100)
    monkeypatch.setattr(fo, "_candidates",
                        lambda: {"dmgs": [{"path": str(dmg), "size": 100}], "caches": [], "node_modules": []})
    prev = await fo.free_space_assist()
    assert prev.requires_confirmation and "dmgs" in prev.user_message

    monkeypatch.setattr(fo, "_resolve_in_home", lambda raw: Path(raw))
    ran = {}

    async def fake_run(args, timeout=20.0):
        ran["args"] = args
        return (0, "")

    monkeypatch.setattr(fo, "_run", fake_run)
    res = await fo.free_space_assist(confirmed=True)
    assert res.data["moved"] == 1 and "Finder" in ran["args"][2]


# ---- D: rename_batch --------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_batch_preview_then_apply(monkeypatch, tmp_path) -> None:
    (tmp_path / "a.heic").write_text("x")
    (tmp_path / "b.heic").write_text("y")
    (tmp_path / "c.txt").write_text("z")
    monkeypatch.setattr(fo, "_resolve_in_home", lambda raw: tmp_path)  # tmp isn't under $HOME

    prev = await fo.rename_batch(".heic", ".jpg", str(tmp_path))
    assert prev.requires_confirmation and prev.data["count"] == 2

    res = await fo.rename_batch(".heic", ".jpg", str(tmp_path), confirmed=True)
    assert res.data["renamed"] == 2
    assert (tmp_path / "a.jpg").exists() and (tmp_path / "b.jpg").exists()
    assert (tmp_path / "c.txt").exists()  # non-matching untouched


@pytest.mark.asyncio
async def test_rename_batch_skips_existing_target(monkeypatch, tmp_path) -> None:
    (tmp_path / "a.heic").write_text("x")
    (tmp_path / "a.jpg").write_text("already")  # collision → skipped at plan time
    monkeypatch.setattr(fo, "_resolve_in_home", lambda raw: tmp_path)
    await fo.rename_batch(".heic", ".jpg", str(tmp_path), confirmed=True)
    assert (tmp_path / "a.heic").exists()  # not renamed
    assert (tmp_path / "a.jpg").read_text() == "already"  # never overwritten


async def _async(value):
    return value


def test_file_edit_denies_dotenv_family(monkeypatch, tmp_path):
    # audit fix: .env.local / .env.production must be refused like .env.
    from tools import file_edit
    monkeypatch.setattr(file_edit, "_home", lambda: tmp_path)
    assert file_edit._resolve_in_home(str(tmp_path / ".env.local")) is None
    assert file_edit._resolve_in_home(str(tmp_path / ".env.production")) is None
    assert file_edit._resolve_in_home(str(tmp_path / ".env")) is None
    assert file_edit._resolve_in_home(str(tmp_path / "notes.txt")) is not None  # normal file ok
