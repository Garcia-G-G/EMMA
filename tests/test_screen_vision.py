"""Part D — core.screen_vision AX bridge, with a mocked AX layer.

Everything funnels through screen_vision._attr; we monkeypatch that single seam
to read from fake elements, so these run with no display and no live AX.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import tools.screen_vision_tool as svt
from core import screen_vision as sv


class FakeAX:
    """A fake AX element: an attribute dict + children, read via _attr."""

    def __init__(self, role="", title="", value=None, subrole="", desc="", children=None):
        self.attrs = {
            sv.kAXRoleAttribute: role,
            sv.kAXTitleAttribute: title,
            sv.kAXValueAttribute: value,
            sv.kAXSubroleAttribute: subrole,
            sv.kAXDescriptionAttribute: desc,
            sv.kAXChildrenAttribute: children or [],
        }


@pytest.fixture(autouse=True)
def _mock_ax(monkeypatch):
    monkeypatch.setattr(sv, "_attr", lambda elem, attr: elem.attrs.get(attr) if elem else None)


def _depth(node: dict) -> int:
    kids = node.get("children") or []
    return 1 + max((_depth(k) for k in kids), default=0)


# ---- read_window_tree -------------------------------------------------------


def test_tree_flattens_role_title_value() -> None:
    win = FakeAX(role="AXWindow", title="Login", children=[
        FakeAX(role="AXButton", title="Aceptar"),
        FakeAX(role="AXStaticText", value="Bienvenido"),
    ])
    tree = sv.read_window_tree(win)
    assert tree["role"] == "AXWindow" and tree["title"] == "Login"
    roles = {c["role"] for c in tree["children"]}
    assert roles == {"AXButton", "AXStaticText"}
    static = next(c for c in tree["children"] if c["role"] == "AXStaticText")
    assert static["value"] == "Bienvenido"


def test_tree_respects_depth_cap() -> None:
    # Build a 6-deep chain; with max_depth=4 the returned dict can't nest past it.
    node = FakeAX(role="AXButton", title="leaf")
    for i in range(6):
        node = FakeAX(role="AXGroup", title=f"g{i}", children=[node])
    tree = sv.read_window_tree(node, max_depth=4)
    assert _depth(tree) <= 5  # root(0) + children to depth 4 = at most 5 nesting levels


def test_tree_respects_node_cap() -> None:
    win = FakeAX(role="AXWindow", children=[FakeAX(role="AXButton", title=f"b{i}") for i in range(500)])
    tree = sv.read_window_tree(win, max_nodes=400)
    assert len(tree["children"]) <= 400
    assert tree.get("truncated") is True


# ---- secret-field omission --------------------------------------------------


def test_secure_field_value_is_hidden_by_role() -> None:
    f = FakeAX(role="AXSecureTextField", value="hunter2")
    tree = sv.read_window_tree(f)
    assert tree["value"] == "<hidden>"
    assert "hunter2" not in str(tree)


def test_secure_field_value_is_hidden_by_subrole() -> None:
    f = FakeAX(role="AXTextField", subrole="AXSecureTextField", value="s3cret")
    assert sv._value_str(f) == "<hidden>"
    assert "s3cret" not in sv.read_text_of(f)


# ---- find_element -----------------------------------------------------------


def test_find_element_by_role_and_title() -> None:
    win = FakeAX(role="AXWindow", children=[
        FakeAX(role="AXButton", title="Cancelar"),
        FakeAX(role="AXGroup", children=[FakeAX(role="AXButton", title="Aceptar")]),
    ])
    ref = sv.find_element(win, role="AXButton", title_re="acept")
    assert ref is not None and ref.title == "Aceptar"


def test_find_element_returns_none_when_absent() -> None:
    win = FakeAX(role="AXWindow", children=[FakeAX(role="AXButton", title="Cancelar")])
    assert sv.find_element(win, role="AXButton", title_re="^Guardar$") is None


# ---- read_text_of -----------------------------------------------------------


def test_read_text_concatenates_and_skips_hidden() -> None:
    win = FakeAX(role="AXWindow", children=[
        FakeAX(role="AXStaticText", value="Hola"),
        FakeAX(role="AXSecureTextField", value="nope"),
        FakeAX(role="AXStaticText", value="mundo"),
    ])
    txt = sv.read_text_of(win)
    assert "Hola" in txt and "mundo" in txt
    assert "nope" not in txt


# ---- structured screen format ----------------------------------------------


def test_format_screen_labels_by_role_and_hides_password() -> None:
    win = FakeAX(role="AXWindow", title="Banco — Login", children=[
        FakeAX(role="AXButton", title="Iniciar sesión"),
        FakeAX(role="AXButton", title="Cancelar"),
        FakeAX(role="AXTextField", title="Usuario", value="garcia2024"),
        FakeAX(role="AXSecureTextField", title="Password", value="hunter2"),
        FakeAX(role="AXStaticText", value="Por seguridad ingrese sus datos"),
    ])
    out = sv._format_screen("Safari", win)
    assert "App: Safari" in out
    assert 'Window: "Banco — Login"' in out
    assert "Iniciar sesión" in out and "Cancelar" in out
    assert "garcia2024" in out
    assert "Password: <hidden>" in out
    assert "hunter2" not in out
    assert "Por seguridad" in out


def test_button_labels_lists_all_buttons() -> None:
    win = FakeAX(role="AXWindow", children=[
        FakeAX(role="AXButton", title="Uno"),
        FakeAX(role="AXGroup", children=[FakeAX(role="AXButton", title="Dos")]),
    ])
    assert sv.button_labels(win) == ["Uno", "Dos"]


# ---- actions ----------------------------------------------------------------


def test_press_element_sends_axpress(monkeypatch) -> None:
    sent = {}
    monkeypatch.setattr(sv, "_AX_OK", True)
    monkeypatch.setattr(sv, "AXUIElementPerformAction", lambda e, a: sent.update(elem=e, action=a) or 0, raising=False)
    ref = sv.ElementRef(elem=FakeAX(role="AXButton"), role="AXButton", title="Aceptar", path="root/AXButton[0]")
    assert sv.press_element(ref) is True
    assert sent["action"] == sv.kAXPressAction


def test_set_element_value_sets_kaxvalue(monkeypatch) -> None:
    sent = {}
    monkeypatch.setattr(sv, "_AX_OK", True)
    monkeypatch.setattr(sv, "AXUIElementSetAttributeValue", lambda e, a, v: sent.update(attr=a, val=v) or 0, raising=False)
    ref = sv.ElementRef(elem=FakeAX(role="AXTextField"), role="AXTextField", title="Usuario", path="root")
    assert sv.set_element_value(ref, "prueba") is True
    assert sent["val"] == "prueba"


# ---- voice tools (Part B) ---------------------------------------------------


def _screen(**kw):
    base = dict(app="Safari", title="Banco — Login", buttons=["Iniciar sesión", "Cancelar"],
                fields=["Usuario: g"], texts=["Por seguridad"], structured="App: Safari\nWindow: ...")
    base.update(kw)
    return sv.ScreenRead(**base)


@pytest.mark.asyncio
async def test_describe_screen_summarizes(monkeypatch) -> None:
    monkeypatch.setattr(sv, "current_screen", AsyncMock(return_value=_screen()))
    res = await svt.describe_screen()
    assert res.success
    assert "Safari" in res.user_message
    assert res.data["screen"]


@pytest.mark.asyncio
async def test_describe_screen_no_window(monkeypatch) -> None:
    monkeypatch.setattr(sv, "current_screen", AsyncMock(return_value=None))
    res = await svt.describe_screen()
    assert not res.success


@pytest.mark.asyncio
async def test_find_button_found(monkeypatch) -> None:
    monkeypatch.setattr(sv, "_frontmost_window_element", lambda: ("Safari", object()))
    monkeypatch.setattr(sv, "button_labels", lambda win: ["Aceptar", "Cancelar"])
    res = await svt.find_button("aceptar")
    assert res.success and "Aceptar" in res.user_message


@pytest.mark.asyncio
async def test_find_button_absent(monkeypatch) -> None:
    monkeypatch.setattr(sv, "_frontmost_window_element", lambda: ("Safari", object()))
    monkeypatch.setattr(sv, "button_labels", lambda win: ["Cancelar"])
    res = await svt.find_button("Guardar todo y salir")
    assert not res.success


@pytest.mark.asyncio
async def test_click_button_requires_confirmation_first(monkeypatch) -> None:
    monkeypatch.setattr(sv, "_frontmost_window_element", lambda: ("Safari", object()))
    monkeypatch.setattr(sv, "button_labels", lambda win: ["Cancelar"])
    res = await svt.click_button("cancelar")
    assert res.requires_confirmation
    assert "Cancelar" in res.user_message


@pytest.mark.asyncio
async def test_click_button_confirmed_presses(monkeypatch) -> None:
    monkeypatch.setattr(sv, "_frontmost_window_element", lambda: ("Safari", object()))
    monkeypatch.setattr(sv, "button_labels", lambda win: ["Cancelar"])
    ref = sv.ElementRef(elem=object(), role="AXButton", title="Cancelar", path="p")
    monkeypatch.setattr(sv, "find_element", lambda *a, **k: ref)
    pressed = {}
    monkeypatch.setattr(sv, "press_element", lambda r: pressed.update(done=True) or True)
    res = await svt.click_button("cancelar", confirmed=True)
    assert res.success and pressed.get("done")


@pytest.mark.asyncio
async def test_type_in_field_never_echoes_text(monkeypatch) -> None:
    monkeypatch.setattr(sv, "_frontmost_window_element", lambda: ("Safari", object()))
    res = await svt.type_in_field("Password", "hunter2")
    assert res.requires_confirmation
    assert "hunter2" not in res.user_message  # secret value must never be echoed


@pytest.mark.asyncio
async def test_summarize_screen_uses_llm(monkeypatch) -> None:
    monkeypatch.setattr(sv, "current_screen", AsyncMock(return_value=_screen(app="Mail")))
    monkeypatch.setattr(svt, "_summarize", AsyncMock(return_value="Tienes Mail abierto."))
    res = await svt.summarize_screen("¿qué hay?")
    assert res.success and "Mail" in res.user_message
