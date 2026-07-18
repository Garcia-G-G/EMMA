"""Emma menubar app (EMMA-APP Part 2) — status item + window, live state.

Runs as its own process (`python -m emma.ui`). A background thread holds a
WebSocket to the daemon's dashboard (`ws://127.0.0.1:{PORT+1}/events`, the bus
that already exists) and drives the menubar icon from `state` events; the main
thread runs Cocoa. UI mutations always hop back to the main thread via
`AppHelper.callAfter` (`evaluateJavaScript`/AppKit are main-thread-only).

Part 3 adds the actionable menu items + the UI→daemon control channel; this part
is the scaffold: state-driven icon, the WKWebView window, and "Abrir Emma".

Verify on-device (cannot be checked headless): the icon appears in the menubar
with no Dock icon, renders in dark AND light mode (setTemplate), and flips
idle/listening/speaking/snoozing/muted as the daemon publishes state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading

import objc
import structlog
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSImage,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSURL, NSURLRequest
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration

log = structlog.get_logger("emma.ui")

_PORT = int(os.environ.get("EMMA_DASHBOARD_PORT", "3200"))
_HTTP_URL = f"http://127.0.0.1:{_PORT}/"
_WS_URL = f"ws://127.0.0.1:{_PORT + 1}/events"
_CONTROL_URL = f"ws://127.0.0.1:{_PORT + 1}/control"


def send_control(payload: dict) -> None:
    """Fire a UI control command at the daemon over the loopback /control socket.

    Runs on a short-lived background thread so the click returns instantly; the
    daemon's reply (state) also arrives on the /events stream, which repaints the
    icon. This is the reverse channel that lets the menubar UNMUTE a mic that voice
    can't reach (EMMA-APP Part 3 closes the EMMA-OBVIOUS hole).
    """

    async def _run() -> None:
        import websockets

        try:
            async with websockets.connect(_CONTROL_URL) as ws:
                await ws.send(json.dumps(payload))
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(ws.recv(), timeout=2.0)  # ack, best-effort
        except Exception as exc:
            log.warning("ui_control_failed", cmd=payload.get("cmd"), error=str(exc))

    threading.Thread(target=lambda: asyncio.run(_run()), daemon=True).start()

# state (from events_bus) -> SF Symbol name. setTemplate makes it adapt to
# light/dark automatically (mandatory — without it the glyph breaks in dark mode).
_ICON_FOR_STATE = {
    "idle": "circle",
    "listening": "waveform",
    "speaking": "waveform.circle.fill",
    "snoozing": "moon",
    "muted": "mic.slash",
}
# Daemon state words -> our icon buckets. Unknown states fall back to idle.
_STATE_BUCKET = {
    "waiting_for_wake": "idle",
    "listening": "listening",
    "speaking": "speaking",
    "responding": "speaking",
    "snoozing": "snoozing",
    "muted": "muted",
}
_ESTADO_LABEL = {
    "idle": "En espera",
    "listening": "Escuchando",
    "speaking": "Hablando",
    "snoozing": "Durmiendo",
    "muted": "Micrófono apagado",
}


class EmmaBar(NSObject):
    """The menubar status item + its window. Main-thread only."""

    def initWithPort_(self, port: int) -> EmmaBar:  # noqa: N802
        self = objc.super(EmmaBar, self).init()
        if self is None:
            return None
        self._port = port
        self._window = None
        self._webview = None
        self._state = "idle"
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self._build_menu()
        self.setState_("idle")
        return self

    # ---- icon -------------------------------------------------------------
    @objc.python_method
    def _apply_icon(self, state: str) -> None:
        symbol = _ICON_FOR_STATE.get(state, "circle")
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, "Emma")
        if img is not None:
            img.setTemplate_(True)  # MANDATORY for dark-mode correctness
            self.item.button().setImage_(img)
        if self._estado_item is not None:
            self._estado_item.setTitle_(f"Emma · {_ESTADO_LABEL.get(state, 'En espera')}")

    def setState_(self, state: str) -> None:  # noqa: N802 (called via callAfter)
        self._state = state
        self._apply_icon(state)
        # Reflect mute state in the toggle label so one item mutes AND unmutes.
        if getattr(self, "_mute_item", None) is not None:
            self._mute_item.setTitle_(
                "Reactivar micrófono" if state == "muted" else "Silenciar micrófono"
            )

    # ---- menu -------------------------------------------------------------
    @objc.python_method
    def _build_menu(self) -> None:
        menu = NSMenu.alloc().init()
        self._estado_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Emma · En espera", None, ""
        )
        self._estado_item.setEnabled_(False)
        menu.addItem_(self._estado_item)
        menu.addItem_(NSMenuItem.separatorItem())
        self._add_item(menu, "Abrir Emma", "openWindow:", "o")
        self._add_item(menu, "Parar", "stopSpeaking:", ".")  # cut Emma mid-sentence
        self._mute_item = self._add_item(menu, "Silenciar micrófono", "toggleMute:", "")
        self._add_item(menu, "Dormir 15 min", "sleep15:", "")
        menu.addItem_(NSMenuItem.separatorItem())
        self._add_item(menu, "Apagar Emma", "shutdownEmma:", "")
        self._add_item(menu, "Salir de esta ventana", "quitUI:", "q")
        self.item.setMenu_(menu)
        self._menu = menu

    # ---- actions (a physical click is trusted input; it is the confirmation) ---
    def stopSpeaking_(self, _sender) -> None:  # noqa: N802
        send_control({"cmd": "stop"})

    def toggleMute_(self, _sender) -> None:  # noqa: N802
        # If the mic is off, this is the way back voice can't give (DoD item 9).
        send_control({"cmd": "unmute" if self._state == "muted" else "mute"})

    def sleep15_(self, _sender) -> None:
        send_control({"cmd": "snooze", "minutes": 15})

    def shutdownEmma_(self, _sender) -> None:  # noqa: N802
        alert = NSAlert.alloc().init()
        alert.setMessageText_("¿Apagar Emma?")
        alert.setInformativeText_(
            "Dejará de escuchar hasta que la reinicies a mano."
        )
        alert.addButtonWithTitle_("Apagar")
        alert.addButtonWithTitle_("Cancelar")
        if alert.runModal() == NSAlertFirstButtonReturn:
            send_control({"cmd": "shutdown"})

    @objc.python_method
    def _add_item(self, menu, title: str, selector: str, key: str = ""):
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, key)
        it.setTarget_(self)
        menu.addItem_(it)
        return it

    # ---- window -----------------------------------------------------------
    def openWindow_(self, _sender) -> None:  # noqa: N802
        if self._window is not None:
            self._window.makeKeyAndOrderFront_(None)
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            return
        rect = NSMakeRect(0, 0, 940, 640)
        mask = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, mask, NSBackingStoreBuffered, False
        )
        win.setTitle_("Emma")
        win.center()
        config = WKWebViewConfiguration.alloc().init()
        webview = WKWebView.alloc().initWithFrame_configuration_(rect, config)
        # Load over http://127.0.0.1, NEVER file:// — a file origin is "null" and
        # the page's WebSocket to the dashboard fails origin checks.
        webview.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(_HTTP_URL)))
        win.setContentView_(webview)
        win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._window = win
        self._webview = webview

    def quitUI_(self, _sender) -> None:  # noqa: N802
        # Closes the UI process only — the daemon (launchd) keeps running.
        NSApplication.sharedApplication().terminate_(None)


class _StateListener(threading.Thread):
    """Background WS client: daemon state events -> the menubar icon.

    Its own asyncio loop in a daemon thread; every UI touch hops to the main
    thread via AppHelper.callAfter. Reconnects with capped backoff so the icon
    recovers when the daemon (or its dashboard) restarts.
    """

    def __init__(self, bar: EmmaBar, ws_url: str) -> None:
        super().__init__(daemon=True)
        self._bar = bar
        self._ws_url = ws_url

    def run(self) -> None:
        asyncio.run(self._loop())

    async def _loop(self) -> None:
        import websockets

        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    backoff = 1.0
                    async for raw in ws:
                        self._on_message(raw)
            except Exception as exc:  # daemon down / dashboard restarting
                log.debug("ui_ws_reconnect", error=str(exc), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 16.0)

    def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("type") != "state":
            return
        bucket = _STATE_BUCKET.get(msg.get("state", ""), "idle")
        # AppKit is main-thread only.
        AppHelper.callAfter(self._bar.setState_, bucket)


def main() -> int:
    app = NSApplication.sharedApplication()
    # Menubar-only: no Dock icon, no app-switcher entry.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    bar = EmmaBar.alloc().initWithPort_(_PORT)
    _StateListener(bar, _WS_URL).start()
    log.info("emma_ui_started", http=_HTTP_URL, ws=_WS_URL)
    AppHelper.runEventLoop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
