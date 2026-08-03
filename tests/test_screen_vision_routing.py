"""Prompt 27.3 — smart AX→screenshot fallback routing.

The chaining decision lives at the LLM layer (system prompt). What we can test
deterministically is the *contract* the prompt keys on: the `density` signal each
AX read attaches, and that a thin AX read leaves look_at_screen reachable with the
same question. No live model, no display — the AX + OCR seams are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import tools.screen_vision_tool as svt
import tools.visual_screen_tool as vst
from core import screen_vision as sv
from core import visual_screen as vis


def _screen(**kw) -> sv.ScreenRead:
    base = dict(
        app="Safari", title="Doc", buttons=[], fields=[], texts=[],
        structured="App: Safari", web_content=False, bounds=None,
    )
    base.update(kw)
    return sv.ScreenRead(**base)


def _pane(**kw) -> sv.PaneInfo:
    base = dict(
        app="Cursor", role="AXGroup", role_description="grupo", identifier="",
        title="editor", label="Editor", position="centro", bounds=None,
        focused_role="AXTextArea", focused_role_description="área de texto",
        snippet="", ancestors=[],
    )
    base.update(kw)
    return sv.PaneInfo(**base)


# ---- Part A: the density signal ---------------------------------------------


@pytest.mark.asyncio
async def test_thin_ax_read_flags_appears_thin(monkeypatch) -> None:
    # < 80 chars of content → thin, and (not a terminal) → fallback is warranted.
    monkeypatch.setattr(sv, "current_screen", AsyncMock(return_value=_screen(texts=["hola"])))
    res = await svt.describe_screen()
    d = res.data["density"]
    assert d["ax_appears_thin"] is True
    assert d["thin_by_design"] is False
    assert d["ax_chars"] < 80


@pytest.mark.asyncio
async def test_rich_ax_read_is_not_thin(monkeypatch) -> None:
    texts = [f"Línea de contenido número {i} con bastante texto real." for i in range(10)]
    buttons = [f"Botón {i}" for i in range(10)]
    monkeypatch.setattr(sv, "current_screen", AsyncMock(return_value=_screen(texts=texts, buttons=buttons)))
    res = await svt.describe_screen()
    d = res.data["density"]
    assert d["ax_appears_thin"] is False
    assert d["ax_chars"] >= 80 and d["ax_buttons"] == 10 and d["ax_static_text"] == 10


@pytest.mark.asyncio
async def test_terminal_is_thin_but_not_fallback_worthy(monkeypatch) -> None:
    # Empty AX tree in a terminal: density reads thin, but thin_by_design tells the
    # LLM the thinness is expected → do NOT chain a screenshot.
    monkeypatch.setattr(sv, "current_screen", AsyncMock(return_value=_screen(app="Terminal", texts=[], buttons=[])))
    res = await svt.describe_screen()
    d = res.data["density"]
    assert d["ax_appears_thin"] is True
    assert d["thin_by_design"] is True


@pytest.mark.asyncio
async def test_large_window_few_lines_is_thin(monkeypatch) -> None:
    # Lots of visible area, almost no AX content (e.g. an image window): thin.
    monkeypatch.setattr(
        sv, "current_screen",
        AsyncMock(return_value=_screen(app="Preview", texts=["IMG_0421.png"], bounds=(0, 0, 1200, 900))),
    )
    res = await svt.describe_screen()
    d = res.data["density"]
    assert d["ax_appears_thin"] is True and d["thin_by_design"] is False


@pytest.mark.asyncio
async def test_pane_read_attaches_density(monkeypatch) -> None:
    # A real code pane is content-rich → not thin, no fallback.
    code = "\n".join(f"def f{i}(): return {i}" for i in range(12))
    monkeypatch.setattr(sv, "focused_pane", lambda: _pane(snippet=code))
    res = await svt.read_pane_text()
    d = res.data["density"]
    assert d["ax_appears_thin"] is False and d["ax_lines"] >= 3


# ---- Part C integration: thin read → look_at_screen is reachable ------------


@pytest.mark.asyncio
async def test_thin_describe_then_look_at_screen_chains(monkeypatch) -> None:
    """Simulate the LLM's layered decision: describe_screen comes back thin (and not
    thin-by-design) so the model chains to look_at_screen with the same question,
    which OCRs the PDF the AX tree never exposed."""
    monkeypatch.setattr(
        sv, "current_screen",
        AsyncMock(return_value=_screen(app="Preview", texts=["Abrir"], bounds=(0, 0, 1100, 800))),
    )
    ax = await svt.describe_screen()
    d = ax.data["density"]
    # The signal the system prompt branches on:
    assert d["ax_appears_thin"] is True and d["thin_by_design"] is False

    # The model now falls back. OCR seam mocked to return the PDF's text.
    monkeypatch.setattr(
        vis, "read_screen",
        AsyncMock(return_value=vis.VisualRead(
            app="Preview", text="Contrato de arrendamiento\nCláusula 1...", line_count=2, scope="window")),
    )
    ocr = await vst.look_at_screen()  # no question → returns the raw OCR text
    assert ocr.success
    assert "Contrato de arrendamiento" in ocr.user_message


# ---- 27.4: web_content_visible + the expanded thin-by-design set ------------
#
# Prompt 27.4 was written but never landed: `git log` on the screen-vision files
# stops at 27.3 (a04bdf8) + the flatten patch (04b0591), `web_content_visible`
# had zero hits repo-wide, and _THIN_BY_DESIGN_APPS was still terminals-only.


@pytest.mark.asyncio
async def test_thin_web_read_flags_web_content_visible(monkeypatch) -> None:
    """Notion-shaped: a WebView is present but AX exposed only the chrome.
    The read did not fail — it lied — so a second AX attempt is wasted."""
    monkeypatch.setattr(
        sv, "current_screen",
        AsyncMock(return_value=_screen(app="Notion", texts=["Notion"], web_content=True,
                                       bounds=(0, 0, 1400, 900))),
    )
    d = (await svt.describe_screen()).data["density"]
    assert d["web_content_visible"] is True
    assert d["ax_chars"] < 200
    assert d["ax_appears_thin"] is True
    assert d["thin_by_design"] is False  # Notion must NOT be suppressed


@pytest.mark.asyncio
async def test_rich_web_read_is_not_flagged_for_fallback(monkeypatch) -> None:
    """A page AX genuinely exposed. web_content alone must not force a capture."""
    texts = [f"Párrafo {i} del artículo con bastante contenido real." for i in range(12)]
    monkeypatch.setattr(
        sv, "current_screen",
        AsyncMock(return_value=_screen(app="Safari", texts=texts, web_content=True)),
    )
    d = (await svt.describe_screen()).data["density"]
    assert d["web_content_visible"] is True
    assert d["ax_chars"] > 500
    assert d["ax_appears_thin"] is False


@pytest.mark.asyncio
async def test_native_app_read_has_web_content_visible_false(monkeypatch) -> None:
    monkeypatch.setattr(
        sv, "current_screen",
        AsyncMock(return_value=_screen(app="Finder", texts=["Documentos", "Descargas"])),
    )
    assert (await svt.describe_screen()).data["density"]["web_content_visible"] is False


@pytest.mark.asyncio
async def test_web_content_visible_is_reported_in_density_only(monkeypatch) -> None:
    """27.4 anti-pattern: pick one path. It lives in density, not at top level."""
    monkeypatch.setattr(
        sv, "current_screen",
        AsyncMock(return_value=_screen(app="Notion", texts=["Notion"], web_content=True)),
    )
    data = (await svt.describe_screen()).data
    assert data["density"]["web_content_visible"] is True
    assert "web_content_visible" not in data


@pytest.mark.asyncio
async def test_pane_read_threads_web_content_through(monkeypatch) -> None:
    monkeypatch.setattr(sv, "focused_pane", lambda: _pane(app="Linear", snippet="Issue",
                                                          web_content=True))
    d = (await svt.read_pane_text()).data["density"]
    assert d["web_content_visible"] is True


def test_canvas_apps_are_thin_by_design() -> None:
    for app in ("figma", "blender", "photoshop", "illustrator", "miro", "lucidchart"):
        assert app in svt._THIN_BY_DESIGN_APPS, app


def test_chat_and_doc_apps_are_not_thin_by_design() -> None:
    """Slack/Discord/Notion/Linear read thin, but their AX still gives useful
    content (channel, page title, recent messages). Marking them thin-by-design
    would suppress the fallback exactly where it helps — they route via the
    web_content_visible rule instead."""
    for app in ("slack", "discord", "notion", "linear"):
        assert app not in svt._THIN_BY_DESIGN_APPS, app


def test_thin_by_design_set_stays_small() -> None:
    """A claim that AX has nothing to give — not a list of apps that read poorly."""
    assert len(svt._THIN_BY_DESIGN_APPS) <= 20


@pytest.mark.asyncio
async def test_system_prompt_carries_the_web_content_routing_rule() -> None:
    """Part C: the routing lives in the prompt, so assert it is actually there."""
    from core import conversation

    prompt = await conversation._build_instructions()
    assert "web_content_visible" in prompt
    assert "ax_chars" in prompt
    assert "look_at_screen" in prompt
