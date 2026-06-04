"""Native macOS window wrapping the dashboard visualizer.

Runs as a SEPARATE subprocess (`python -m emma.visualizer_window`) so its
NSApp main-thread loop never collides with Emma's daemon asyncio loop. The
window is borderless-in-appearance (titlebar hidden, transparent), always-on-top,
and draggable from anywhere on its content. Closes with Cmd+W.

Connects to the dashboard via WKWebView pointed at:
    http://localhost:{EMMA_DASHBOARD_PORT}/visualizer

19.6-B22 — black-screen fix. Two stacked causes observed:
  1. WebKit's content process can lose the race against window-show on a
     cold start, leaving the (black) window background visible.
  2. If dashboard/server.py isn't running, the load fails silently and the
     window stays black forever.
Fixes applied: a centered "CARGANDO…" placeholder label sits BEHIND the
webview (visible until the page's opaque body paints over it), and a render
watchdog polls the page's ``window.__emma_painted`` sentinel (set on
DOMContentLoaded in visualizer.html) ~2s after load: not painted + not
loading → ONE reload (``visualizer_black_screen_retried``); painted after
the retry → ``visualizer_repaint_recovered``; still dead → an inline
diagnostic page replaces the black void. Capped at one retry — no flicker
loops. (The original 15.11 plan assumed ``loadFileURL``; the live loader is
an HTTP ``loadRequest``, which is why "server down" is failure mode #1.)

Env vars:
    EMMA_DASHBOARD_PORT (default 3200): dashboard HTTP port.
    EMMA_VISUALIZER_SCALE (default 0.55): window side as fraction of min(screen W, H).
    EMMA_VISUALIZER_ALWAYS_ON_TOP (default 1): set 0 to disable floating level.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import objc
import structlog
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSColor,
    NSFloatingWindowLevel,
    NSFont,
    NSMakeRect,
    NSNormalWindowLevel,
    NSScreen,
    NSTextField,
    NSView,
    NSViewHeightSizable,
    NSViewMaxXMargin,
    NSViewMaxYMargin,
    NSViewMinXMargin,
    NSViewMinYMargin,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
)
from Foundation import NSURL, NSObject, NSTimer, NSURLRequest
from WebKit import WKWebView, WKWebViewConfiguration

log = structlog.get_logger("emma.visualizer_window")

_PAINT_SENTINEL_JS = "window.__emma_painted === true"
_FIRST_CHECK_S = 2.0  # cold WebKit usually paints well under this
_RECHECK_S = 1.5

_FALLBACK_HTML = """<!doctype html><html><body style="margin:0;background:#03070d;
color:#5cf2ff;font-family:ui-monospace,monospace;display:flex;align-items:center;
justify-content:center;height:100vh;text-align:center">
<div><div style="font-size:14px;letter-spacing:.3em">EMMA · SIN SEÑAL</div>
<div style="opacity:.6;margin-top:12px;font-size:11px">El dashboard no responde
en el puerto {port}.<br>Arranca <code>dashboard/server.py</code> y vuelve a abrir
la ventana.</div></div></body></html>"""


class _RenderWatchdog(NSObject):  # type: ignore[misc]  # PyObjC base is Any
    """One-shot reload if the visualizer never painted (19.6-B22, option A).

    State machine: check sentinel at ~2s → still loading? re-check → not
    painted? reload ONCE and re-check → still dead? swap in the diagnostic
    page. Never more than one reload (anti-flicker contract).
    """

    def initWithWebview_request_port_(self, webview: Any, request: Any, port: int) -> Any:  # noqa: N802
        self = objc.super(_RenderWatchdog, self).init()
        if self is None:
            return None
        self._webview = webview
        self._request = request
        self._port = port
        self._retried = False
        return self

    @objc.python_method
    def start(self) -> None:
        self._schedule(_FIRST_CHECK_S)

    @objc.python_method
    def _schedule(self, delay: float) -> None:
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            delay, self, "tick:", None, False
        )

    def tick_(self, _timer: Any) -> None:
        def _handler(result: Any, error: Any) -> None:
            painted = bool(result) and error is None
            if painted:
                if self._retried:
                    log.info("visualizer_repaint_recovered")
                return
            if self._webview.isLoading():
                self._schedule(_RECHECK_S)  # give the in-flight load time to land
                return
            if not self._retried:
                self._retried = True
                log.warning("visualizer_black_screen_retried", retry=1)
                self._webview.loadRequest_(self._request)
                self._schedule(_FIRST_CHECK_S + _RECHECK_S)
                return
            log.error("visualizer_load_failed_after_retry", port=self._port)
            self._webview.loadHTMLString_baseURL_(
                _FALLBACK_HTML.replace("{port}", str(self._port)), None
            )

        self._webview.evaluateJavaScript_completionHandler_(_PAINT_SENTINEL_JS, _handler)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() in ("1", "true", "yes")


def _loading_placeholder(rect: Any) -> Any:
    """Centered label BEHIND the webview — visible until the page paints over
    it (19.6-B22, option B: a black frame becomes an intentional state)."""
    label = NSTextField.labelWithString_("EMMA · CARGANDO…")
    label.setTextColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(0.36, 0.95, 1.0, 0.8))
    label.setFont_(NSFont.monospacedSystemFontOfSize_weight_(13.0, 0.3))
    label.sizeToFit()
    lf = label.frame()
    label.setFrameOrigin_(
        ((rect.size.width - lf.size.width) / 2, (rect.size.height - lf.size.height) / 2)
    )
    label.setAutoresizingMask_(
        NSViewMinXMargin | NSViewMaxXMargin | NSViewMinYMargin | NSViewMaxYMargin
    )
    return label


def main() -> int:
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

    screen = NSScreen.mainScreen().frame()
    scale = max(0.25, min(0.95, _env_float("EMMA_VISUALIZER_SCALE", 0.55)))
    side = min(screen.size.width, screen.size.height) * scale
    rect = NSMakeRect(
        (screen.size.width - side) / 2,
        (screen.size.height - side) / 2,
        side,
        side,
    )

    mask = (
        NSWindowStyleMaskTitled
        | NSWindowStyleMaskClosable
        | NSWindowStyleMaskResizable
        | NSWindowStyleMaskFullSizeContentView
    )
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, mask, NSBackingStoreBuffered, False
    )
    window.setTitle_("Emma · Core")
    window.setTitlebarAppearsTransparent_(True)
    window.setTitleVisibility_(NSWindowTitleHidden)
    window.setMovableByWindowBackground_(True)
    # Intended dark, not stock black: an unpainted frame blends in (B22-B).
    window.setBackgroundColor_(
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.012, 0.027, 0.05, 1.0)
    )
    window.setOpaque_(True)
    window.setLevel_(
        NSFloatingWindowLevel
        if _env_bool("EMMA_VISUALIZER_ALWAYS_ON_TOP", True)
        else NSNormalWindowLevel
    )

    bounds = NSMakeRect(0, 0, rect.size.width, rect.size.height)
    container = NSView.alloc().initWithFrame_(bounds)
    container.addSubview_(_loading_placeholder(bounds))

    config = WKWebViewConfiguration.alloc().init()
    webview = WKWebView.alloc().initWithFrame_configuration_(bounds, config)
    webview.setValue_forKey_(False, "drawsBackground")  # transparent fallback
    webview.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    container.addSubview_(webview)
    window.setContentView_(container)

    port = int(os.environ.get("EMMA_DASHBOARD_PORT", "3200"))
    url = NSURL.URLWithString_(f"http://localhost:{port}/visualizer")
    request = NSURLRequest.requestWithURL_(url)
    webview.loadRequest_(request)

    watchdog = _RenderWatchdog.alloc().initWithWebview_request_port_(webview, request, port)
    watchdog.start()

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
