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
from typing import Any
from urllib.parse import quote

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
    NSSecureTextField,
    NSStatusBar,
    NSTextField,
    NSVariableStatusItemLength,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWorkspace,
)
from Foundation import NSURL, NSURLRequest
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration

from core import control_auth

log = structlog.get_logger("emma.ui")

_PORT = int(os.environ.get("EMMA_DASHBOARD_PORT", "3200"))
# The daemon's control-channel token, handed down through the
# environment (LAUNCH-11 Part 2). It rides in the query string of every socket
# this process opens, and in the URL the WebView is pointed at — the page reads
# it from location.search. It is deliberately NOT embedded in the served HTML,
# because any local process can fetch that.
_TOKEN = quote(control_auth.token(), safe="")
_HTTP_URL = f"http://127.0.0.1:{_PORT}/?t={_TOKEN}"
_WS_URL = f"ws://127.0.0.1:{_PORT + 1}/events?token={_TOKEN}"
_CONTROL_URL = f"ws://127.0.0.1:{_PORT + 1}/control?token={_TOKEN}"


def send_control(payload: dict[str, object]) -> None:
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
    "thinking": "ellipsis",
    "speaking": "waveform.circle.fill",
    "snoozing": "moon",
    "muted": "mic.slash",
    # Out of minutes: she is alive and listening but cannot answer until the
    # user tops up. A distinct glyph, because "en espera" would be a lie.
    "out_of_minutes": "exclamationmark.circle",
}
# Daemon state words -> our icon buckets. Unknown states fall back to idle.
_STATE_BUCKET = {
    "waiting_for_wake": "idle",
    "listening": "listening",
    "thinking": "thinking",
    "speaking": "speaking",
    "responding": "speaking",
    "snoozing": "snoozing",
    "muted": "muted",
    "out_of_minutes": "out_of_minutes",
}
_ESTADO_LABEL = {
    "idle": "En espera",
    "listening": "Escuchando",
    "thinking": "Pensando",
    "speaking": "Hablando",
    "snoozing": "Durmiendo",
    "muted": "Micrófono apagado",
    "out_of_minutes": "Sin minutos",
}


class _WebUIDelegate(NSObject):  # type: ignore[misc]
    """WKUIDelegate for the app window's web view (LAUNCH-10 Part 3).

    Deliberately NOT declared with ``protocols=[objc.protocolNamed("WKUIDelegate")]``.
    That was tried, and it makes ``conformsToProtocol_`` report True while the
    confirm panel stops being delivered at all — worse than the bug being fixed.
    PyObjC matches these by selector name and resolves the block signatures from
    the WebKit metadata; ``respondsToSelector_`` confirms all three are
    registered, and the behavior is verified end-to-end in a real WKWebView (see
    _planning/notes/LAUNCH-10-VERIFY.md). Do not "tidy" this into a formal
    conformance without re-running that verification.

    A WKWebView with no UI delegate silently no-ops the three things the
    onboarding flow is built out of. Nothing throws; nothing logs; the buttons
    just do nothing:

      * ``window.open(url, "_blank")`` returns null — so "Abrir el navegador",
        both "Comprar minutos" buttons and "Abrir panel web" were all dead, and
        the pairing hand-off had no way to reach the browser at all.
      * ``confirm()`` returns false immediately — so "Desvincular" always read
        as "the user said no" and silently did nothing.

    WebKit routes both through this delegate, and only if one is set.

    The browser rule is unchanged and deliberate: pairing goes to the SYSTEM
    browser, never this web view. It is the full web auth stack, OAuth included,
    and a password must never be typed into a window Emma owns.
    """

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(  # noqa: N802
        self, _webview: Any, _config: Any, action: Any, _features: Any
    ) -> None:
        """`window.open` / target=_blank → hand the URL to the system browser.

        Returning None (rather than a new WKWebView) tells WebKit not to open a
        popup inside the app, which is exactly what we want: the URL leaves for
        Safari/Chrome and the app window stays on the onboarding pane, polling.
        """
        url = None
        with contextlib.suppress(Exception):
            url = action.request().URL()
        if url is None:
            log.warning("webview_open_no_url")
            return None
        log.info("webview_open_external", url=str(url.absoluteString()))
        NSWorkspace.sharedWorkspace().openURL_(url)
        return None

    def webView_runJavaScriptConfirmPanelWithMessage_initiatedByFrame_completionHandler_(  # noqa: N802
        self, _webview: Any, message: str, _frame: Any, handler: Any
    ) -> None:
        alert = NSAlert.alloc().init()
        alert.setMessageText_(str(message))
        alert.addButtonWithTitle_("Continuar")
        alert.addButtonWithTitle_("Cancelar")
        handler(alert.runModal() == NSAlertFirstButtonReturn)

    def webView_runJavaScriptAlertPanelWithMessage_initiatedByFrame_completionHandler_(  # noqa: N802
        self, _webview: Any, message: str, _frame: Any, handler: Any
    ) -> None:
        # Implemented for completeness: an unhandled alert() does not merely
        # no-op, it leaves WebKit waiting on a panel nobody will ever dismiss.
        alert = NSAlert.alloc().init()
        alert.setMessageText_(str(message))
        alert.addButtonWithTitle_("OK")
        alert.runModal()
        handler()


class _ScriptBridge(NSObject):  # type: ignore[misc]
    """Page -> host process channel for actions that must NOT cross a socket.

    ``WKScriptMessageHandler`` is a direct call from the page into the process
    that owns the WebView. It is authenticated by construction — nothing else
    can post to it — which is exactly what entering an API key needs, and
    exactly what the loopback control channel could not offer before it was
    given a token (audit P1-13).

    The key therefore travels: secure text field -> this process -> Keychain.
    It never enters the page, never enters a WebSocket frame, and never reaches
    the daemon.
    """

    def initWithBar_(self, bar: Any) -> Any:  # noqa: N802
        self = objc.super(_ScriptBridge, self).init()
        if self is None:
            return None
        self._bar = bar
        return self

    def userContentController_didReceiveScriptMessage_(  # noqa: N802
        self, _controller: Any, message: Any
    ) -> None:
        try:
            action = str(message.body())
        except Exception:
            return
        if action == "enter_key":
            self._bar.promptForKey_(None)
        elif action == "clear_key":
            self._bar.clearKey_(None)
        elif action == "enter_license":
            self._bar.promptForLicense_(None)
        else:
            log.warning("ui_unknown_script_message", action=action[:40])


class EmmaBar(NSObject):  # type: ignore[misc]
    """The menubar status item + its window. Main-thread only."""

    def initWithPort_(self, port: int) -> EmmaBar:  # noqa: N802
        self = objc.super(EmmaBar, self).init()
        if self is None:
            return None
        self._port = port
        self._window = None
        self._webview = None
        self._ui_delegate = None
        self._bridge = None
        self._state = "idle"
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self._build_menu()
        self.setState_("idle")
        return self

    # ---- icon -------------------------------------------------------------
    @objc.python_method  # type: ignore[untyped-decorator]
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
    @objc.python_method  # type: ignore[untyped-decorator]
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
        # A second, always-available entry point for the key — the onboarding
        # fork is the first, but someone who skipped it must not have to reinstall.
        self._add_item(menu, "Mi API key de OpenAI…", "promptForKey:", "")
        self._add_item(menu, "Activar mi licencia…", "promptForLicense:", "")
        self._add_item(menu, "Dar acceso a la pantalla…", "grantAccessibility:", "")
        self._add_item(menu, "Apagar Emma", "shutdownEmma:", "")
        self._add_item(menu, "Salir de esta ventana", "quitUI:", "q")
        self.item.setMenu_(menu)
        self._menu = menu

    # ---- actions (a physical click is trusted input; it is the confirmation) ---
    def stopSpeaking_(self, _sender: Any) -> None:  # noqa: N802
        send_control({"cmd": "stop"})

    def toggleMute_(self, _sender: Any) -> None:  # noqa: N802
        # If the mic is off, this is the way back voice can't give (DoD item 9).
        send_control({"cmd": "unmute" if self._state == "muted" else "mute"})

    def sleep15_(self, _sender: Any) -> None:
        send_control({"cmd": "snooze", "minutes": 15})

    def grantAccessibility_(self, _sender: Any) -> None:  # noqa: N802
        # Re-request Accessibility from the app if it was declined (LAUNCH-2.1). The
        # daemon runs it, so the macOS alert + the Settings row belong to Emma.
        send_control({"cmd": "request_accessibility"})

    def shutdownEmma_(self, _sender: Any) -> None:  # noqa: N802
        alert = NSAlert.alloc().init()
        alert.setMessageText_("¿Apagar Emma?")
        alert.setInformativeText_(
            "Dejará de escuchar hasta que la reinicies a mano."
        )
        alert.addButtonWithTitle_("Apagar")
        alert.addButtonWithTitle_("Cancelar")
        if alert.runModal() == NSAlertFirstButtonReturn:
            send_control({"cmd": "shutdown"})

    @objc.python_method  # type: ignore[untyped-decorator]
    def _add_item(self, menu: Any, title: str, selector: str, key: str = "") -> Any:
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, key)
        it.setTarget_(self)
        menu.addItem_(it)
        return it

    # ---- window -----------------------------------------------------------
    def openWindow_(self, _sender: Any) -> None:  # noqa: N802
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
        # Page -> host bridge for the API key (LAUNCH-11 Part 2). Held on self:
        # WKUserContentController keeps only a weak reference to the handler.
        self._bridge = _ScriptBridge.alloc().initWithBar_(self)
        config.userContentController().addScriptMessageHandler_name_(self._bridge, "emma")
        # Popups must be allowed to REACH the UI delegate. Without this the
        # window.open below is refused before createWebView... is ever called.
        config.preferences().setJavaScriptCanOpenWindowsAutomatically_(True)
        webview = WKWebView.alloc().initWithFrame_configuration_(rect, config)
        # Without a UI delegate, window.open() returns null and confirm() returns
        # false, silently — which killed the pairing hand-off, both "Comprar
        # minutos" buttons, "Abrir panel web" and "Desvincular" (LAUNCH-10 Part 3).
        # Held on self: WebKit keeps only a weak reference, so a delegate that
        # falls out of scope leaves us exactly where we started.
        self._ui_delegate = _WebUIDelegate.alloc().init()
        webview.setUIDelegate_(self._ui_delegate)
        # Load over http://127.0.0.1, NEVER file:// — a file origin is "null" and
        # the page's WebSocket to the dashboard fails origin checks.
        webview.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(_HTTP_URL)))
        win.setContentView_(webview)
        win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._window = win
        self._webview = webview

    # ---- BYO key (LAUNCH-11 Part 2) ---------------------------------------
    def promptForKey_(self, _sender: Any) -> None:  # noqa: N802
        """Native secure input. The key goes field -> Keychain, nothing between.

        Deliberately NOT an HTML field: the page would have to hand the value to
        this process somehow, and every available route (the loopback control
        socket, a fetch to the daemon) puts a Secret-tier value on an IPC channel
        for no benefit. An NSSecureTextField also gets the OS behaviours for free
        — no screen-capture of the contents, no autofill, no spellcheck upload.
        """
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Tu API key de OpenAI")
        alert.setInformativeText_(
            "Se guarda en el Keychain de tu Mac. Emma nunca la envía a sus "
            "servidores: las llamadas van directo de tu Mac a OpenAI.\n\n"
            "Tú le pagas a OpenAI por lo que uses. Emma no puede decirte cuánto "
            "gastaste — eso lo ves en tu panel de OpenAI."
        )
        field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 24))
        field.setPlaceholderString_("sk-…")
        alert.setAccessoryView_(field)
        alert.addButtonWithTitle_("Guardar")
        alert.addButtonWithTitle_("Cancelar")
        alert.window().setInitialFirstResponder_(field)

        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        key = str(field.stringValue()).strip()
        field.setStringValue_("")  # do not leave it in the view's buffer
        if not key:
            return
        self._set_key_status("Verificando con OpenAI…")
        threading.Thread(target=lambda: self._validate_and_store(key), daemon=True).start()

    @objc.python_method  # type: ignore[untyped-decorator]
    def _validate_and_store(self, key: str) -> None:
        """Off the main thread: validate direct with OpenAI, then store."""
        from core import byok

        async def _run() -> tuple[bool, str]:
            ok, msg = await byok.validate(key)
            if ok:
                await byok.store(key)
            return ok, msg

        try:
            ok, msg = asyncio.run(_run())
        except Exception as exc:
            # Never include the exception text verbatim — it can carry the key.
            log.warning("byok_store_failed", error_type=type(exc).__name__)
            ok, msg = False, "No pude guardar la key. Intenta de nuevo."
        AppHelper.callAfter(lambda: self._finish_key(ok, msg))

    @objc.python_method  # type: ignore[untyped-decorator]
    def _finish_key(self, ok: bool, msg: str) -> None:
        self._set_key_status(msg)
        if not ok:
            return
        send_control({"cmd": "mode_changed"})  # let the daemon re-resolve its tier
        self._eval_js("window.__emmaKeySaved && window.__emmaKeySaved();")

    def promptForLicense_(self, _sender: Any) -> None:  # noqa: N802
        """Licence key entry. A plain field — this is a purchase token, not a
        credential to anyone's account — but it goes through the same native
        path so there is exactly one way into the app's secrets handling."""
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Tu clave de licencia")
        alert.setInformativeText_(
            "La recibiste al comprar Emma. Se verifica una vez; después Emma "
            "funciona aunque estés sin internet."
        )
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 340, 24))
        field.setPlaceholderString_("EMMA-XXXX-XXXX-XXXX")
        alert.setAccessoryView_(field)
        alert.addButtonWithTitle_("Activar")
        alert.addButtonWithTitle_("Ahora no")
        alert.window().setInitialFirstResponder_(field)
        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        key = str(field.stringValue()).strip()
        if not key:
            return
        self._set_license_status("Verificando…")
        threading.Thread(target=lambda: self._activate_license(key), daemon=True).start()

    @objc.python_method  # type: ignore[untyped-decorator]
    def _activate_license(self, key: str) -> None:
        from core import license as lic

        try:
            _ok, msg = asyncio.run(lic.activate(key))
        except Exception as exc:
            log.warning("license_activate_failed", error_type=type(exc).__name__)
            # Fail OPEN, here too: an exception in our own code must not read as
            # "unlicensed" to the user staring at the dialog.
            msg = "No pude verificar ahora. Emma funciona; lo reintento después."
        AppHelper.callAfter(lambda: self._set_license_status(msg))

    @objc.python_method  # type: ignore[untyped-decorator]
    def _set_license_status(self, text: str) -> None:
        self._eval_js(
            f"window.__emmaLicenseStatus && window.__emmaLicenseStatus({json.dumps(text)});"
        )

    def clearKey_(self, _sender: Any) -> None:  # noqa: N802
        from core import byok

        alert = NSAlert.alloc().init()
        alert.setMessageText_("¿Quitar tu API key?")
        alert.setInformativeText_(
            "Emma dejará de usar tu key. Podrás volver a ponerla cuando quieras."
        )
        alert.addButtonWithTitle_("Quitar")
        alert.addButtonWithTitle_("Cancelar")
        if alert.runModal() != NSAlertFirstButtonReturn:
            return

        def _work() -> None:
            with contextlib.suppress(Exception):
                asyncio.run(byok.clear())
            AppHelper.callAfter(lambda: self._set_key_status("Key eliminada."))

        threading.Thread(target=_work, daemon=True).start()

    @objc.python_method  # type: ignore[untyped-decorator]
    def _set_key_status(self, text: str) -> None:
        self._eval_js(f"window.__emmaKeyStatus && window.__emmaKeyStatus({json.dumps(text)});")

    @objc.python_method  # type: ignore[untyped-decorator]
    def _eval_js(self, script: str) -> None:
        if self._webview is None:
            return
        with contextlib.suppress(Exception):
            self._webview.evaluateJavaScript_completionHandler_(script, None)

    def quitUI_(self, _sender: Any) -> None:  # noqa: N802
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

    def _on_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        # A valid-but-non-object frame (123, "x", []) from a buggy/hostile stream
        # must not raise here — that would tear down the socket and loop-reconnect.
        if not isinstance(msg, dict) or msg.get("type") != "state":
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
