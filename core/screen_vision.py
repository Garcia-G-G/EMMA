"""Screen vision via the macOS Accessibility (AX) API — read what's on screen.

This is the same UI-element store VoiceOver uses: every app publishes a tree of
``AXUIElement`` nodes (windows → groups → buttons / text fields / static text)
with attributes (role, title, value, position). We read it natively — no OCR,
no screenshots. The Accessibility TCC grant is requested at install
(``core/permissions.py``), so reads need no new prompt.

Design notes:
- Every AX C call funnels through :func:`_attr`, the single seam, so the
  tree-walk logic is unit-testable with a mocked AX layer (no live display).
- Trees are read FRESH per call (AX state changes every keystroke) and capped
  (depth ≤ 4, nodes ≤ 400) — some apps have monstrous trees.
- Secure fields (passwords) NEVER have their value read — role/subrole carrying
  "Secure" is reported as ``<hidden>``.
- All blocking AX work runs in a worker thread (``read_current_screen``).
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger("emma.screen_vision")

# AX symbols. Guarded so the module imports (and unit tests run) even where the
# framework or a display is unavailable; the public API degrades to empty/None.
# Attribute / action names. Stable AX identifier strings (these literals ARE the
# values of Apple's kAX* constants); we define them here so the module imports
# and unit tests run even where the framework or a display is absent.
kAXChildrenAttribute = "AXChildren"  # noqa: N816 - Apple AX constant name
kAXDescriptionAttribute = "AXDescription"  # noqa: N816
kAXEnabledAttribute = "AXEnabled"  # noqa: N816
kAXFocusedUIElementAttribute = "AXFocusedUIElement"  # noqa: N816
kAXFocusedWindowAttribute = "AXFocusedWindow"  # noqa: N816
kAXIdentifierAttribute = "AXIdentifier"  # noqa: N816
kAXParentAttribute = "AXParent"  # noqa: N816
kAXPositionAttribute = "AXPosition"  # noqa: N816
kAXPressAction = "AXPress"  # noqa: N816
kAXRoleAttribute = "AXRole"  # noqa: N816
kAXRoleDescriptionAttribute = "AXRoleDescription"  # noqa: N816
kAXSizeAttribute = "AXSize"  # noqa: N816
kAXSubroleAttribute = "AXSubrole"  # noqa: N816
kAXTitleAttribute = "AXTitle"  # noqa: N816
kAXValueAttribute = "AXValue"  # noqa: N816
# Opt-in signals that make an app expose its embedded web-content AX subtree:
# Safari/WebKit honour AXEnhancedUserInterface; Chromium (Chrome/Brave/Edge/Arc)
# + Electron only populate their tree once an AT requests it via
# AXManualAccessibility. Set on the APPLICATION element, once per process.
kAXEnhancedUserInterfaceAttribute = "AXEnhancedUserInterface"  # noqa: N816
kAXManualAccessibilityAttribute = "AXManualAccessibility"  # noqa: N816

try:
    from ApplicationServices import (
        AXUIElementCopyAttributeValue,
        AXUIElementCreateApplication,
        AXUIElementPerformAction,
        AXUIElementSetAttributeValue,
    )

    _AX_OK = True
except Exception as exc:  # pragma: no cover - only on a box without the framework
    log.warning("ax_unavailable", error=str(exc))
    _AX_OK = False

# Role strings (stable AX identifiers).
ROLE_WINDOW = "AXWindow"
ROLE_BUTTON = "AXButton"
ROLE_TEXTFIELD = "AXTextField"
ROLE_TEXTAREA = "AXTextArea"
ROLE_SECURE = "AXSecureTextField"
ROLE_STATIC = "AXStaticText"
ROLE_WEBAREA = "AXWebArea"  # the embedded web-content root inside browsers/Electron

_MAX_DEPTH = 4
_MAX_NODES = 400
_TEXT_CAP = 4000  # never hand the LLM an unbounded wall of text
# When a walk reaches an AXWebArea, refresh its subtree's depth allowance by this
# much so page content isn't dead-ended by chrome already spending the base depth.
# The global node cap (_MAX_NODES) still bounds total work — this only re-opens
# DEPTH, never the node budget (some pages are thousands of nodes).
_WEBAREA_EXTRA_DEPTH = 6


@dataclass(frozen=True)
class WindowSnapshot:
    app: str
    title: str
    role: str
    bounds: tuple[float, float, float, float] | None  # x, y, w, h
    focused_role: str | None
    focused_title: str | None


@dataclass(frozen=True)
class ElementRef:
    """Opaque handle to an AX element + a human path for logging/re-lookup."""

    elem: Any
    role: str
    title: str
    path: str


# ---- the single AX seam -----------------------------------------------------


def _attr(elem: Any, attr: str) -> Any | None:
    """Read one AX attribute. Returns None on any error / non-success.

    THE mock seam: unit tests monkeypatch this to read from fake elements, so
    nothing below ever touches the real C API in tests.
    """
    if elem is None or not _AX_OK:
        return None
    try:
        err, value = AXUIElementCopyAttributeValue(elem, attr, None)
        return value if err == 0 else None
    except Exception:
        return None


def _role(elem: Any) -> str:
    return str(_attr(elem, kAXRoleAttribute) or "")


def _subrole(elem: Any) -> str:
    return str(_attr(elem, kAXSubroleAttribute) or "")


def _title(elem: Any) -> str:
    return str(_attr(elem, kAXTitleAttribute) or "")


def _description(elem: Any) -> str:
    return str(_attr(elem, kAXDescriptionAttribute) or "")


def _children(elem: Any) -> list[Any]:
    kids = _attr(elem, kAXChildrenAttribute)
    return list(kids) if kids else []


def _is_secure(elem: Any) -> bool:
    return "Secure" in _role(elem) or "Secure" in _subrole(elem)


def _value_str(elem: Any) -> str:
    """String value of an element, or "<hidden>" for a secure (password) field."""
    if _is_secure(elem):
        return "<hidden>"
    val = _attr(elem, kAXValueAttribute)
    if val is None:
        return ""
    if isinstance(val, bool):
        return ""
    return str(val)


def _bounds(elem: Any) -> tuple[float, float, float, float] | None:
    pos = _attr(elem, kAXPositionAttribute)
    size = _attr(elem, kAXSizeAttribute)
    try:
        if pos is not None and size is not None:
            return (float(pos.x), float(pos.y), float(size.width), float(size.height))
    except Exception:
        return None
    return None


# ---- frontmost app ----------------------------------------------------------


def _frontmost_app() -> Any | None:
    """The frontmost ``NSRunningApplication`` (has localizedName + pid)."""
    try:
        from AppKit import NSWorkspace

        return NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception as exc:  # pragma: no cover
        log.warning("frontmost_app_failed", error=str(exc))
        return None


# PIDs whose enhanced-UI flag we've already handled this session → verdict.
# Module scope: resets when the daemon restarts (no persistence). Generic — keyed
# by PID, never by app name. Bounded LRU: the macOS PID space recycles, so a
# stale entry could wrongly mark a NEW app (reused PID) as already-handled, and an
# unbounded dict leaks over a multi-day daemon (audit fix). 256 covers any real
# working set of concurrently-running apps.
_ENHANCED_SEEN_MAX = 256
_enhanced_seen: OrderedDict[int, bool] = OrderedDict()


def _ensure_enhanced_ui(app_elem: Any, pid: int) -> bool:
    """Flip the attribute that makes an app expose its web-content AX subtree.

    Generic, app-agnostic: try AXEnhancedUserInterface (WebKit), then
    AXManualAccessibility (Chromium/Electron). Set ONCE per process — the flag
    persists for the app's lifetime. NEVER raises: apps that don't support these
    attributes simply expose whatever they expose natively. Returns whether a
    flag stuck (cached per PID so we don't re-set on every read).
    """
    if not _AX_OK or app_elem is None:
        return False
    if pid in _enhanced_seen:
        _enhanced_seen.move_to_end(pid)  # LRU touch
        return _enhanced_seen[pid]  # already handled — don't re-set
    ok = False
    for attr in (kAXEnhancedUserInterfaceAttribute, kAXManualAccessibilityAttribute):
        try:
            if AXUIElementSetAttributeValue(app_elem, attr, True) == 0:
                ok = True
                log.debug("enhanced_ui_enabled", pid=pid, attr=attr)
                break
        except Exception:
            continue  # unsupported attribute — try the next, never raise
    if not ok:
        log.debug("enhanced_ui_refused", pid=pid)
    _enhanced_seen[pid] = ok
    _enhanced_seen.move_to_end(pid)
    while len(_enhanced_seen) > _ENHANCED_SEEN_MAX:
        _enhanced_seen.popitem(last=False)  # evict oldest
    return ok


def _app_element(app: Any) -> Any | None:
    if app is None or not _AX_OK:
        return None
    try:
        pid = app.processIdentifier()
        elem = AXUIElementCreateApplication(pid)
    except Exception:
        return None
    # Once per process, ask the app to expose its embedded web/Electron tree so
    # every read path below (chrome AND page content) sees through the wall.
    _ensure_enhanced_ui(elem, pid)
    return elem


def _focused_window(app_elem: Any) -> Any | None:
    return _attr(app_elem, kAXFocusedWindowAttribute)


# ---- public read API --------------------------------------------------------


def frontmost_window() -> WindowSnapshot | None:
    """Snapshot of the frontmost app's focused window, or None."""
    app = _frontmost_app()
    if app is None:
        return None
    app_elem = _app_element(app)
    win = _focused_window(app_elem)
    if win is None:
        return None
    focused = _attr(app_elem, "AXFocusedUIElement")
    return WindowSnapshot(
        app=str(app.localizedName() or ""),
        title=_title(win),
        role=_role(win) or ROLE_WINDOW,
        bounds=_bounds(win),
        focused_role=_role(focused) if focused is not None else None,
        focused_title=(_title(focused) or _description(focused)) if focused is not None else None,
    )


def read_window_tree(
    elem: Any, max_depth: int = _MAX_DEPTH, max_nodes: int = _MAX_NODES
) -> dict[str, Any]:
    """DFS the AX tree from ``elem`` into a capped role/title/value dict."""
    budget = [max_nodes]

    def walk(node: Any, depth: int, depth_cap: int) -> dict[str, Any]:
        out: dict[str, Any] = {"role": _role(node), "title": _title(node)}
        text = _value_str(node)
        if text:
            out["value"] = text[:_TEXT_CAP]
        desc = _description(node)
        if desc:
            out["description"] = desc[:200]
        # A web area refreshes its subtree's depth so page content is reachable
        # even when window chrome already spent the base depth budget.
        if out["role"] == ROLE_WEBAREA:
            depth_cap = max(depth_cap, depth + 1 + _WEBAREA_EXTRA_DEPTH)
        if depth < depth_cap:
            kids: list[dict[str, Any]] = []
            for child in _children(node):
                if budget[0] <= 0:
                    out["truncated"] = True
                    break
                budget[0] -= 1
                kids.append(walk(child, depth + 1, depth_cap))
            if kids:
                out["children"] = kids
        return out

    return walk(elem, 0, max_depth)


def find_element(
    elem: Any,
    role: str | None = None,
    title_re: str | None = None,
    description_re: str | None = None,
    max_depth: int = _MAX_DEPTH,
    max_nodes: int = _MAX_NODES,
) -> ElementRef | None:
    """First element under ``elem`` matching role/title/description, DFS, capped."""
    title_pat = re.compile(title_re, re.IGNORECASE) if title_re else None
    desc_pat = re.compile(description_re, re.IGNORECASE) if description_re else None
    budget = [max_nodes]

    def matches(node: Any) -> bool:
        if role is not None and _role(node) != role:
            return False
        if title_pat is not None and not title_pat.search(_title(node)):
            return False
        return desc_pat is None or bool(desc_pat.search(_description(node)))

    def walk(node: Any, depth: int, path: str, depth_cap: int) -> ElementRef | None:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        if matches(node):
            return ElementRef(elem=node, role=_role(node), title=_title(node), path=path)
        if _role(node) == ROLE_WEBAREA:
            depth_cap = max(depth_cap, depth + 1 + _WEBAREA_EXTRA_DEPTH)
        if depth < depth_cap:
            for i, child in enumerate(_children(node)):
                hit = walk(child, depth + 1, f"{path}/{_role(child) or '?'}[{i}]", depth_cap)
                if hit is not None:
                    return hit
        return None

    return walk(elem, 0, _role(elem) or "root", max_depth)


def _read_text_and_web(elem: Any, cap: int = _TEXT_CAP) -> tuple[str, bool]:
    """Concatenate value + description + descendants' text (capped), and report
    whether any AXWebArea was crossed — i.e. this is web/editor page content."""
    parts: list[str] = []
    budget = [_MAX_NODES]
    saw_web = [False]

    def walk(node: Any) -> None:
        if budget[0] <= 0:
            return
        budget[0] -= 1
        if _role(node) == ROLE_WEBAREA:
            saw_web[0] = True
        v = _value_str(node)
        if v and v != "<hidden>":
            parts.append(v)
        d = _description(node)
        if d:
            parts.append(d)
        for child in _children(node):
            if sum(len(p) for p in parts) >= cap:
                return
            walk(child)

    walk(elem)
    return " ".join(parts)[:cap].strip(), saw_web[0]


def read_text_of(elem: Any, _cap: int = _TEXT_CAP) -> str:
    """Concatenate value + description + descendants' text, capped."""
    return _read_text_and_web(elem, _cap)[0]


# ---- high-level structured read ---------------------------------------------


def _collect(win: Any) -> dict[str, Any]:
    """Walk the window collecting buttons / fields / static text by role.

    Returns the role buckets plus ``web_content`` — True when an AXWebArea was
    crossed, so callers can tell page content from app chrome. Reaching a web
    area refreshes the depth budget so its page text isn't dead-ended by chrome.
    """
    buttons: list[str] = []
    fields: list[str] = []
    texts: list[str] = []
    web = [False]
    budget = [_MAX_NODES]

    def walk(node: Any, depth: int, depth_cap: int) -> None:
        if budget[0] <= 0:
            return
        budget[0] -= 1
        role = _role(node)
        if role == ROLE_WEBAREA:
            web[0] = True
            depth_cap = max(depth_cap, depth + 1 + _WEBAREA_EXTRA_DEPTH)
        if role == ROLE_BUTTON:
            label = _title(node) or _description(node)
            if label:
                buttons.append(label)
        elif role in (ROLE_TEXTFIELD, ROLE_TEXTAREA, ROLE_SECURE) or _is_secure(node):
            label = _title(node) or _description(node) or "campo"
            fields.append(f"{label}: {_value_str(node)}")
        elif role == ROLE_STATIC:
            t = _value_str(node) or _title(node)
            if t:
                texts.append(t)
        if depth < depth_cap:
            for child in _children(node):
                if budget[0] <= 0:
                    break
                walk(child, depth + 1, depth_cap)

    walk(win, 0, _MAX_DEPTH)
    return {"buttons": buttons, "fields": fields, "texts": texts, "web_content": web[0]}


def _format_screen(app_name: str, win: Any) -> str:
    title = _title(win)
    parts = _collect(win)
    lines = [f"App: {app_name}", f'Window: "{title}"']
    if parts["buttons"]:
        lines.append("Buttons: [" + ", ".join(parts["buttons"][:30]) + "]")
    if parts["fields"]:
        lines.append("Fields: [" + ", ".join(parts["fields"][:20]) + "]")
    if parts["texts"]:
        joined = " · ".join(parts["texts"])[:_TEXT_CAP]
        lines.append(f'Text: "{joined}"')
    return "\n".join(lines)


@dataclass(frozen=True)
class ScreenRead:
    app: str
    title: str
    buttons: list[str]
    fields: list[str]
    texts: list[str]
    structured: str  # the flattened App/Window/Buttons/Fields/Text block
    web_content: bool = False  # True when the read crossed an embedded web area
    bounds: tuple[float, float, float, float] | None = None  # window x, y, w, h (27.3 density)


def _current_screen_sync() -> ScreenRead | None:
    fw = _frontmost_window_element()
    if fw is None:
        return None
    app, win = fw
    parts = _collect(win)
    return ScreenRead(
        app=app,
        title=_title(win),
        buttons=parts["buttons"],
        fields=parts["fields"],
        texts=parts["texts"],
        structured=_format_screen(app, win),
        web_content=bool(parts["web_content"]),
        bounds=_bounds(win),
    )


async def current_screen() -> ScreenRead | None:
    """Structured read of the frontmost focused window (AX runs off the loop)."""
    return await asyncio.to_thread(_current_screen_sync)


async def read_current_screen() -> str:
    """Structured TEXT of the frontmost focused window — what the LLM reasons over."""
    r = await current_screen()
    return r.structured if r is not None else ""


# ---- actions (used by the destructive tools) --------------------------------


def press_element(ref: ElementRef) -> bool:
    """Send AXPress to an element. True on success."""
    if not _AX_OK:
        return False
    try:
        return bool(AXUIElementPerformAction(ref.elem, kAXPressAction) == 0)
    except Exception as exc:
        log.warning("ax_press_failed", error=str(exc))
        return False


def set_element_value(ref: ElementRef, text: str) -> bool:
    """Set kAXValue on a field. True on success. (Never logs the value.)"""
    if not _AX_OK:
        return False
    try:
        return bool(AXUIElementSetAttributeValue(ref.elem, kAXValueAttribute, text) == 0)
    except Exception as exc:
        log.warning("ax_set_value_failed", error=str(exc))
        return False


def _frontmost_window_element() -> tuple[str, Any] | None:
    """(app_name, focused_window_element) for the tools to search, or None."""
    app = _frontmost_app()
    if app is None:
        return None
    win = _focused_window(_app_element(app))
    if win is None:
        return None
    return (str(app.localizedName() or ""), win)


def button_labels(win: Any) -> list[str]:
    """Every button label in the window (for fuzzy matching in the tools)."""
    return list(_collect(win)["buttons"])


# ---- pane / focus introspection (27.1) --------------------------------------
# Generic: a "pane" is whichever ancestor of the focused element the APP itself
# gave an identity (title / identifier / role description), or — failing any
# label — a distinct sub-region by geometry. No app names, no role/pane-type
# tables: we read only what AX exposes and let the LLM name the region.

# Cocoa/web AX hierarchies from a focused leaf up to its window are typically
# 5-15 levels; 20 covers the deepest real nestings with margin while bounding a
# janky/cyclic parent chain.
_WALK_UP_CAP = 20


def _identifier(elem: Any) -> str:
    return str(_attr(elem, kAXIdentifierAttribute) or "")


def _role_description(elem: Any) -> str:
    return str(_attr(elem, kAXRoleDescriptionAttribute) or "")


def _parent(elem: Any) -> Any | None:
    return _attr(elem, kAXParentAttribute)


def _focused_element(app_elem: Any) -> Any | None:
    return _attr(app_elem, kAXFocusedUIElementAttribute)


def _specific_label(elem: Any) -> str:
    """A name the app deliberately set for a region (title or identifier)."""
    return (_title(elem) or _identifier(elem)).strip()


def _any_label(elem: Any) -> str:
    """Best human-readable identity, falling back to the generic role description."""
    return (_title(elem) or _identifier(elem) or _role_description(elem)).strip()


def _ancestors(elem: Any) -> list[Any]:
    """Walk up via AXParent (capped, cycle-guarded), nearest-first; stop at window."""
    chain: list[Any] = []
    seen: set[int] = set()
    cur = _parent(elem)
    hops = 0
    while cur is not None and hops < _WALK_UP_CAP:
        if id(cur) in seen:
            break
        seen.add(id(cur))
        chain.append(cur)
        if _role(cur) == ROLE_WINDOW:
            break
        cur = _parent(cur)
        hops += 1
    return chain


def _position(pane_b: Any, win_b: Any) -> str:
    """Spanish position of a region within its window — pure geometry, generic."""
    if not pane_b or not win_b or win_b[2] <= 0 or win_b[3] <= 0:
        return ""
    cx = (pane_b[0] + pane_b[2] / 2 - win_b[0]) / win_b[2]
    cy = (pane_b[1] + pane_b[3] / 2 - win_b[1]) / win_b[3]
    horiz = "izquierda" if cx < 0.38 else "derecha" if cx > 0.62 else ""
    vert = "arriba" if cy < 0.38 else "abajo" if cy > 0.62 else ""
    if horiz and vert:
        return f"{vert} a la {horiz}"
    return horiz or vert or "centro"


def _pick_pane(focused: Any, ancestors: list[Any], win_b: Any) -> Any:
    """The region holding focus, chosen generically (identity → geometry):
    1) nearest ancestor with an app-set name (title/identifier);
    2) else nearest ancestor with a role description (generic region kind);
    3) else nearest ancestor that is a distinct sub-region by area;
    4) else the nearest container, or the focused element itself.
    """
    mids = [a for a in ancestors if _role(a) != ROLE_WINDOW]
    for a in mids:
        if _specific_label(a):
            return a
    for a in mids:
        if _role_description(a):
            return a
    if win_b:
        win_area = max(1.0, win_b[2] * win_b[3])
        for a in mids:
            b = _bounds(a)
            if b and (b[2] * b[3]) <= 0.85 * win_area:
                return a
    return mids[0] if mids else focused


@dataclass(frozen=True)
class PaneInfo:
    app: str
    role: str
    role_description: str
    identifier: str
    title: str
    label: str  # best human-readable name the app exposed (title|identifier|roleDesc)
    position: str  # geometric position within the window
    bounds: tuple[float, float, float, float] | None
    focused_role: str
    focused_role_description: str
    snippet: str  # pane text, secure-skipped, capped
    ancestors: list[str]  # "role: label" nearest→outermost, raw context for the LLM
    web_content: bool = False  # True when the pane's text came from an embedded web area


def _resolve_focused_pane() -> tuple[str, Any, Any, Any, list[Any]] | None:
    """(app_name, window, focused_elem, pane_elem, ancestors) or None if the app
    exposes no focused element (e.g. an app with a broken/sparse AX tree)."""
    app = _frontmost_app()
    if app is None:
        return None
    app_elem = _app_element(app)
    focused = _focused_element(app_elem)
    if focused is None:
        return None
    ancestors = _ancestors(focused)
    window = next((a for a in ancestors if _role(a) == ROLE_WINDOW), None) or _focused_window(app_elem)
    pane = _pick_pane(focused, ancestors, _bounds(window))
    return (str(app.localizedName() or ""), window, focused, pane, ancestors)


def focused_pane() -> PaneInfo | None:
    """Structured record of the region holding the user's focus, or None."""
    res = _resolve_focused_pane()
    if res is None:
        return None
    app_name, window, focused, pane, ancestors = res
    win_b = _bounds(window)
    pane_b = _bounds(pane)
    snippet, web = _read_text_and_web(pane, 600)
    return PaneInfo(
        app=app_name,
        role=_role(pane),
        role_description=_role_description(pane),
        identifier=_identifier(pane),
        title=_title(pane),
        label=_any_label(pane),
        position=_position(pane_b, win_b),
        bounds=pane_b,
        focused_role=_role(focused),
        focused_role_description=_role_description(focused),
        snippet=snippet,
        ancestors=[f"{_role(a)}: {_any_label(a)}".strip(" :") for a in ancestors],
        web_content=web,
    )


def focused_pane_element() -> Any | None:
    """The AX element of the resolved focused pane (for scoped button search)."""
    res = _resolve_focused_pane()
    return res[3] if res else None


def window_panes() -> list[dict[str, str]]:
    """Layout map: labeled regions in the frontmost window (shallow DFS), each
    with its geometric position. Deduped by label; no app-specific knowledge."""
    fw = _frontmost_window_element()
    if fw is None:
        return []
    _app, win = fw
    win_b = _bounds(win)
    panes: list[dict[str, str]] = []
    seen: set[str] = set()
    budget = [_MAX_NODES]

    def walk(node: Any, depth: int) -> None:
        if budget[0] <= 0 or depth > 3:
            return
        budget[0] -= 1
        label = _any_label(node)
        if label and label not in seen and _role(node) != ROLE_WINDOW:
            seen.add(label)
            panes.append({"label": label, "role": _role(node), "position": _position(_bounds(node), win_b)})
        for child in _children(node):
            if budget[0] <= 0:
                break
            walk(child, depth + 1)

    walk(win, 0)
    return panes[:20]
