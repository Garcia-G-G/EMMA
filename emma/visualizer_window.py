"""Native macOS window wrapping the dashboard visualizer.

Runs as a SEPARATE subprocess (`python -m emma.visualizer_window`) so its
NSApp main-thread loop never collides with Emma's daemon asyncio loop. The
window is borderless-in-appearance (titlebar hidden, transparent), always-on-top,
and draggable from anywhere on its content. Closes with Cmd+W.

Connects to the dashboard via WKWebView pointed at:
    http://localhost:{EMMA_DASHBOARD_PORT}/visualizer

Env vars:
    EMMA_DASHBOARD_PORT (default 3200): dashboard HTTP port.
    EMMA_VISUALIZER_SCALE (default 0.55): window side as fraction of min(screen W, H).
    EMMA_VISUALIZER_ALWAYS_ON_TOP (default 1): set 0 to disable floating level.
"""

from __future__ import annotations

import os
import sys

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSColor,
    NSFloatingWindowLevel,
    NSMakeRect,
    NSNormalWindowLevel,
    NSScreen,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
)
from Foundation import NSURL, NSURLRequest
from WebKit import WKWebView, WKWebViewConfiguration


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() in ("1", "true", "yes")


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
    window.setBackgroundColor_(NSColor.blackColor())
    window.setOpaque_(True)
    window.setLevel_(
        NSFloatingWindowLevel
        if _env_bool("EMMA_VISUALIZER_ALWAYS_ON_TOP", True)
        else NSNormalWindowLevel
    )

    config = WKWebViewConfiguration.alloc().init()
    webview = WKWebView.alloc().initWithFrame_configuration_(rect, config)
    webview.setValue_forKey_(False, "drawsBackground")  # transparent fallback
    window.setContentView_(webview)

    port = int(os.environ.get("EMMA_DASHBOARD_PORT", "3200"))
    url = NSURL.URLWithString_(f"http://localhost:{port}/visualizer")
    webview.loadRequest_(NSURLRequest.requestWithURL_(url))

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
