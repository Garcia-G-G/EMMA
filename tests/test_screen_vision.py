"""Part D — core.screen_vision AX bridge, with a mocked AX layer.

Everything funnels through screen_vision._attr; we monkeypatch that single seam
to read from fake elements, so these run with no display and no live AX.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import tools.screen_vision_tool as svt
from core import screen_vision as sv


class _Pt:
    def __init__(self, x, y):
        self.x, self.y = x, y


class _Sz:
    def __init__(self, w, h):
        self.width, self.height = w, h


class FakeAX:
    """A fake AX element: an attribute dict + children, read via _attr.

    Children get their AXParent back-pointer wired automatically so the pane
    walk-up works in tests.
    """

    def __init__(self, role="", title="", value=None, subrole="", desc="",
                 identifier="", role_desc="", pos=None, size=None, children=None):
        self.attrs = {
            sv.kAXRoleAttribute: role,
            sv.kAXTitleAttribute: title,
            sv.kAXValueAttribute: value,
            sv.kAXSubroleAttribute: subrole,
            sv.kAXDescriptionAttribute: desc,
            sv.kAXIdentifierAttribute: identifier,
            sv.kAXRoleDescriptionAttribute: role_desc,
            sv.kAXChildrenAttribute: children or [],
            sv.kAXParentAttribute: None,
            sv.kAXPositionAttribute: _Pt(*pos) if pos else None,
            sv.kAXSizeAttribute: _Sz(*size) if size else None,
        }
        for c in children or []:
            c.attrs[sv.kAXParentAttribute] = self


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
        FakeAX(role="AXTextField", title="Usuario", value="alex2024"),
        FakeAX(role="AXSecureTextField", title="Password", value="hunter2"),
        FakeAX(role="AXStaticText", value="Por seguridad ingrese sus datos"),
    ])
    out = sv._format_screen("Safari", win)
    assert "App: Safari" in out
    assert 'Window: "Banco — Login"' in out
    assert "Iniciar sesión" in out and "Cancelar" in out
    assert "alex2024" in out
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


# ---- pane / focus introspection (27.1) --------------------------------------


def test_position_is_pure_geometry() -> None:
    win = (0, 0, 1000, 800)
    assert sv._position((0, 0, 200, 800), win) == "izquierda"
    assert sv._position((800, 0, 200, 800), win) == "derecha"
    assert sv._position((400, 300, 200, 200), win) == "centro"
    assert sv._position((0, 600, 300, 200), win) == "abajo a la izquierda"


def test_ancestors_walk_up_cap_fires() -> None:
    node = FakeAX(role="AXLeaf")
    for i in range(30):  # 30-deep chain; cap is 20
        node = FakeAX(role="AXGroup", title=f"g{i}", children=[node])
    leaf = node
    while sv._children(leaf):
        leaf = sv._children(leaf)[0]
    assert len(sv._ancestors(leaf)) <= sv._WALK_UP_CAP


def test_ancestors_cycle_guard() -> None:
    a = FakeAX(role="AXGroup")
    b = FakeAX(role="AXGroup")
    a.attrs[sv.kAXParentAttribute] = b
    b.attrs[sv.kAXParentAttribute] = a  # cycle
    assert len(sv._ancestors(a)) <= sv._WALK_UP_CAP  # no infinite loop


def test_pick_pane_prefers_app_set_label_over_nearer_generic() -> None:
    win = FakeAX(role="AXWindow")
    labeled = FakeAX(role="AXGroup", title="Terminal")  # app named this region
    generic = FakeAX(role="AXGroup", role_desc="grupo")  # nearer, but unnamed
    focused = FakeAX(role="AXTextArea")
    pane = sv._pick_pane(focused, [generic, labeled, win], (0, 0, 1000, 800))
    assert pane is labeled


def test_pick_pane_geometry_fallback_when_unlabeled() -> None:
    big = FakeAX(role="AXGroup", pos=(0, 0), size=(1000, 800))  # == window
    small = FakeAX(role="AXScrollArea", pos=(700, 0), size=(300, 800))  # distinct sub-region
    focused = FakeAX(role="AXTextArea")
    pane = sv._pick_pane(focused, [small, big], (0, 0, 1000, 800))
    assert pane is small


def _seed_focus(monkeypatch, app_elem, name="TestApp"):
    class _App:
        def localizedName(self):  # noqa: N802 - mirrors AppKit
            return name

        def processIdentifier(self):  # noqa: N802 - mirrors AppKit
            return 1

    monkeypatch.setattr(sv, "_frontmost_app", lambda: _App())
    monkeypatch.setattr(sv, "_app_element", lambda app: app_elem)


def test_focused_pane_resolves_label_position_and_hides_secret(monkeypatch) -> None:
    win = FakeAX(role="AXWindow", pos=(0, 0), size=(1000, 800))
    pane = FakeAX(role="AXGroup", title="Terminal", pos=(0, 500), size=(1000, 300), children=[
        FakeAX(role="AXStaticText", value="$ ls"),
        FakeAX(role="AXSecureTextField", value="hunter2"),
    ])
    focused = FakeAX(role="AXTextArea", value="comando")
    focused.attrs[sv.kAXParentAttribute] = pane
    pane.attrs[sv.kAXParentAttribute] = win
    app_elem = FakeAX(role="AXApplication")
    app_elem.attrs[sv.kAXFocusedUIElementAttribute] = focused
    _seed_focus(monkeypatch, app_elem)

    p = sv.focused_pane()
    assert p is not None
    assert p.label == "Terminal"
    assert p.position == "abajo"
    assert "$ ls" in p.snippet
    assert "hunter2" not in p.snippet  # secure value never leaks into the snippet


def test_focused_pane_none_when_app_exposes_no_focus(monkeypatch) -> None:
    app_elem = FakeAX(role="AXApplication")  # no AXFocusedUIElement (Electron-like)
    _seed_focus(monkeypatch, app_elem)
    assert sv.focused_pane() is None


def test_window_panes_lists_labeled_regions(monkeypatch) -> None:
    win = FakeAX(role="AXWindow", pos=(0, 0), size=(1000, 800), children=[
        FakeAX(role="AXGroup", title="Sidebar", pos=(0, 0), size=(200, 800)),
        FakeAX(role="AXGroup", identifier="editor", pos=(200, 0), size=(800, 500)),
        FakeAX(role="AXScrollArea", role_desc="terminal area", pos=(200, 500), size=(800, 300)),
    ])
    monkeypatch.setattr(sv, "_frontmost_window_element", lambda: ("X", win))
    labels = {p["label"] for p in sv.window_panes()}
    assert {"Sidebar", "editor", "terminal area"} <= labels


# ---- scoped tools -----------------------------------------------------------


@pytest.mark.asyncio
async def test_find_button_scope_focus_narrows_to_pane(monkeypatch) -> None:
    win, pane = object(), object()
    monkeypatch.setattr(sv, "_frontmost_window_element", lambda: ("App", win))
    monkeypatch.setattr(sv, "focused_pane_element", lambda: pane)
    seen = {}

    def labels(root):
        seen["root"] = root
        return ["Enviar"] if root is pane else ["Enviar", "Cancelar", "Guardar"]

    monkeypatch.setattr(sv, "button_labels", labels)
    res = await svt.find_button("Enviar", scope="focus")
    assert res.success and seen["root"] is pane


@pytest.mark.asyncio
async def test_find_button_default_scope_is_whole_window(monkeypatch) -> None:
    win, pane = object(), object()
    monkeypatch.setattr(sv, "_frontmost_window_element", lambda: ("App", win))
    monkeypatch.setattr(sv, "focused_pane_element", lambda: pane)
    seen = {}

    def labels(root):
        seen["root"] = root
        return ["Guardar"]

    monkeypatch.setattr(sv, "button_labels", labels)
    res = await svt.find_button("Guardar")  # 27 behavior, no scope arg
    assert res.success and seen["root"] is win


@pytest.mark.asyncio
async def test_where_am_i_names_the_region(monkeypatch) -> None:
    p = sv.PaneInfo(app="App", role="AXGroup", role_description="grupo", identifier="",
                    title="Agent chat", label="Agent chat", position="derecha", bounds=None,
                    focused_role="AXTextArea", focused_role_description="área de texto",
                    snippet="hola", ancestors=["AXGroup: Agent chat"])
    monkeypatch.setattr(sv, "focused_pane", lambda: p)
    res = await svt.where_am_i()
    assert res.success
    assert "Agent chat" in res.user_message and "derecha" in res.user_message


@pytest.mark.asyncio
async def test_where_am_i_degrades_when_no_focus(monkeypatch) -> None:
    monkeypatch.setattr(sv, "focused_pane", lambda: None)
    monkeypatch.setattr(sv, "current_screen", AsyncMock(return_value=_screen()))
    res = await svt.where_am_i()
    assert res.success  # degrades to the window read rather than failing
    assert res.data["pane"] is None


# ---- 27.2: enhanced-UI flag + deep web-content reads ------------------------


def _enh_app(accepts):
    """A fake app element whose set-attribute succeeds only for `accepts` attrs."""
    sent = []

    def _set(elem, attr, val):
        sent.append(attr)
        return 0 if attr in accepts else -1

    return _set, sent


def test_ensure_enhanced_ui_enables_webkit_flag(monkeypatch) -> None:
    sv._enhanced_seen.clear()
    setter, sent = _enh_app({sv.kAXEnhancedUserInterfaceAttribute})
    monkeypatch.setattr(sv, "_AX_OK", True)
    monkeypatch.setattr(sv, "AXUIElementSetAttributeValue", setter, raising=False)
    assert sv._ensure_enhanced_ui(object(), pid=101) is True
    assert sent == [sv.kAXEnhancedUserInterfaceAttribute]  # stopped at the first that stuck


def test_ensure_enhanced_ui_falls_back_to_chromium_flag(monkeypatch) -> None:
    sv._enhanced_seen.clear()
    setter, sent = _enh_app({sv.kAXManualAccessibilityAttribute})
    monkeypatch.setattr(sv, "_AX_OK", True)
    monkeypatch.setattr(sv, "AXUIElementSetAttributeValue", setter, raising=False)
    assert sv._ensure_enhanced_ui(object(), pid=102) is True
    assert sent == [sv.kAXEnhancedUserInterfaceAttribute, sv.kAXManualAccessibilityAttribute]


def test_ensure_enhanced_ui_returns_false_when_both_refused(monkeypatch) -> None:
    sv._enhanced_seen.clear()
    setter, _ = _enh_app(set())  # accepts nothing
    monkeypatch.setattr(sv, "_AX_OK", True)
    monkeypatch.setattr(sv, "AXUIElementSetAttributeValue", setter, raising=False)
    assert sv._ensure_enhanced_ui(object(), pid=103) is False


def test_ensure_enhanced_ui_never_raises(monkeypatch) -> None:
    sv._enhanced_seen.clear()

    def _boom(elem, attr, val):
        raise RuntimeError("AX exploded")

    monkeypatch.setattr(sv, "_AX_OK", True)
    monkeypatch.setattr(sv, "AXUIElementSetAttributeValue", _boom, raising=False)
    assert sv._ensure_enhanced_ui(object(), pid=104) is False  # swallowed, not raised


def test_ensure_enhanced_ui_skips_when_pid_already_seen(monkeypatch) -> None:
    sv._enhanced_seen.clear()
    calls = []
    monkeypatch.setattr(sv, "_AX_OK", True)
    monkeypatch.setattr(sv, "AXUIElementSetAttributeValue",
                        lambda e, a, v: calls.append(a) or 0, raising=False)
    sv._ensure_enhanced_ui(object(), pid=200)
    n_first = len(calls)
    sv._ensure_enhanced_ui(object(), pid=200)  # same PID → must NOT call the setter again
    assert len(calls) == n_first


def _webarea_tree(depth_below_web: int = 5) -> FakeAX:
    """Window → chrome group → AXWebArea → deep chain ending in page text."""
    leaf = FakeAX(role="AXStaticText", value="CEPHALOPOD_CONTENT")
    node = leaf
    for i in range(depth_below_web):
        node = FakeAX(role="AXGroup", title=f"w{i}", children=[node])
    web = FakeAX(role="AXWebArea", children=[node])
    chrome = FakeAX(role="AXGroup", title="toolbar", children=[web])
    return FakeAX(role="AXWindow", title="Octopus - Wikipedia", children=[chrome])


def test_webarea_refreshes_depth_so_page_text_is_reached() -> None:
    win = _webarea_tree(depth_below_web=5)
    parts = sv._collect(win)
    assert parts["web_content"] is True
    # The deep page text lands despite chrome consuming the base depth budget.
    assert any("CEPHALOPOD_CONTENT" in t for t in parts["texts"])


def test_read_window_tree_reaches_into_webarea() -> None:
    win = _webarea_tree(depth_below_web=5)
    tree = sv.read_window_tree(win)
    assert "CEPHALOPOD_CONTENT" in str(tree)  # reached the page text within the node cap


def test_collect_web_content_false_without_webarea() -> None:
    win = FakeAX(role="AXWindow", children=[FakeAX(role="AXStaticText", value="plain")])
    assert sv._collect(win)["web_content"] is False


def test_secure_field_inside_webarea_is_redacted() -> None:
    web = FakeAX(role="AXWebArea", children=[
        FakeAX(role="AXStaticText", value="public text"),
        FakeAX(role="AXSecureTextField", title="pw", value="hunter2"),
    ])
    win = FakeAX(role="AXWindow", children=[web])
    parts = sv._collect(win)
    blob = str(parts)
    assert "public text" in blob
    assert "hunter2" not in blob  # secure value never leaks, even inside a web area
    txt, web_seen = sv._read_text_and_web(win)
    assert web_seen is True and "hunter2" not in txt


def test_global_node_cap_still_bounds_huge_webarea() -> None:
    # A web area with thousands of children must NOT blow past the node cap.
    huge = FakeAX(role="AXWebArea",
                  children=[FakeAX(role="AXStaticText", value=f"n{i}") for i in range(5000)])
    win = FakeAX(role="AXWindow", children=[huge])
    parts = sv._collect(win)  # must return, capped — no meltdown
    assert len(parts["texts"]) <= sv._MAX_NODES


def test_focused_pane_flags_web_content(monkeypatch) -> None:
    web = FakeAX(role="AXWebArea", children=[FakeAX(role="AXStaticText", value="article body")])
    pane = FakeAX(role="AXGroup", title="content", pos=(0, 0), size=(1000, 800), children=[web])
    focused = FakeAX(role="AXStaticText", value="article body")
    focused.attrs[sv.kAXParentAttribute] = pane
    pane.attrs[sv.kAXParentAttribute] = FakeAX(role="AXWindow", pos=(0, 0), size=(1000, 800))
    app_elem = FakeAX(role="AXApplication")
    app_elem.attrs[sv.kAXFocusedUIElementAttribute] = focused
    _seed_focus(monkeypatch, app_elem)
    p = sv.focused_pane()
    assert p is not None and p.web_content is True
