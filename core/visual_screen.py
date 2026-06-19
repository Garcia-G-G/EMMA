"""On-device visual screen reading: screenshot → Apple Vision OCR.

The companion to core/screen_vision.py (which reads the structured AX tree). This
captures a screenshot and runs Apple's Vision text recognition **fully
on-device** — so Emma can read text inside images, canvases, PDFs, games, and
apps that expose no accessibility tree. Nothing leaves the Mac: no cloud vision.

Privacy: a raw screenshot can't redact secure fields the way the AX read does,
so the image is NEVER sent anywhere — it's OCR'd locally and the temp file is
deleted immediately. Only the recognized text (which the LLM may then summarize,
same as the AX path) leaves, exactly like describe_screen already does today.

Capture is scoped to the frontmost window when we can resolve its id, else the
full display. All blocking work runs off the event loop via ``read_screen``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import tempfile
from dataclasses import dataclass

import structlog

log = structlog.get_logger("emma.visual_screen")

_SCREENCAPTURE = "/usr/sbin/screencapture"
_OCR_LANGUAGES = ["es-ES", "en-US"]
_LINE_CAP = 200  # never hand the LLM an unbounded wall of OCR lines

try:
    import Quartz
    import Vision
    from Foundation import NSData

    _VISION_OK = True
except Exception as exc:  # pragma: no cover - only on a box without the frameworks
    log.warning("vision_unavailable", error=str(exc))
    _VISION_OK = False


def _frontmost_window_id() -> int | None:
    """CGWindowID of the frontmost app's foreground window, or None for full screen."""
    if not _VISION_OK:
        return None
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        pid = app.processIdentifier()
        opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
            if w.get("kCGWindowOwnerPID") == pid and int(w.get("kCGWindowLayer", 0)) == 0:
                num = w.get("kCGWindowNumber")
                return int(num) if num is not None else None
    except Exception as exc:
        log.debug("frontmost_window_id_failed", error=str(exc))
    return None


def _capture(window_id: int | None) -> bytes | None:
    """Screenshot → PNG bytes (window-scoped if id given), temp file deleted after.

    ``-x`` silences the shutter sound; ``-o`` drops the window shadow. Returns
    None on any failure (e.g. Screen Recording permission not granted) — callers
    degrade honestly rather than crash.
    """
    # mkstemp (not mktemp): atomic create owned 0600 by us, no TOCTOU race.
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    args = [_SCREENCAPTURE, "-x", "-t", "png"]
    if window_id is not None:
        args += ["-o", "-l", str(window_id)]
    args.append(path)
    try:
        rc = subprocess.run(args, capture_output=True, timeout=10).returncode
        if rc != 0 or not os.path.exists(path) or os.path.getsize(path) == 0:
            return None
        with open(path, "rb") as f:
            return f.read()
    except Exception as exc:
        log.warning("screencapture_failed", error=str(exc))
        return None
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)  # never leave the screenshot on disk


def _ocr(png: bytes) -> list[str]:
    """Apple Vision text recognition over PNG bytes → lines (top-to-bottom)."""
    if not _VISION_OK or not png:
        return []
    try:
        src = Quartz.CGImageSourceCreateWithData(NSData.dataWithBytes_length_(png, len(png)), None)
        if src is None:
            return []
        img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        if img is None:
            return []
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(1)  # 1 = accurate
        req.setRecognitionLanguages_(_OCR_LANGUAGES)
        req.setUsesLanguageCorrection_(True)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
        handler.performRequests_error_([req], None)
        lines: list[str] = []
        for obs in req.results() or []:
            cand = obs.topCandidates_(1)
            if cand:
                lines.append(str(cand[0].string()))
        return lines[:_LINE_CAP]
    except Exception as exc:
        log.warning("vision_ocr_failed", error=str(exc))
        return []


@dataclass(frozen=True)
class VisualRead:
    app: str
    text: str  # OCR'd text, newline-joined
    line_count: int
    scope: str  # "window" or "screen"


def _app_name() -> str:
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName() or "") if app else ""
    except Exception:
        return ""


def _read_screen_sync() -> VisualRead | None:
    wid = _frontmost_window_id()
    png = _capture(wid)
    if png is None:
        return None
    lines = _ocr(png)
    return VisualRead(
        app=_app_name(),
        text="\n".join(lines).strip(),
        line_count=len(lines),
        scope="window" if wid is not None else "screen",
    )


async def read_screen() -> VisualRead | None:
    """Capture + OCR the frontmost window (off the event loop). None on failure."""
    return await asyncio.to_thread(_read_screen_sync)
