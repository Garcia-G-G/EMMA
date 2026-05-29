"""Open Emma's visual brain (the JARVIS native window)."""

from __future__ import annotations

import asyncio
import os
import sys

from config.settings import settings
from tools.base import ToolResult, tool


@tool()
async def show_visualizer() -> ToolResult:
    """Open Emma's visual interface — the JARVIS-style native window — and bring it forward.

    Call this for ANY request to show Emma's visual self / interface / brain / HUD,
    in Spanish or English. Triggers include: "abre tu cerebro", "abre tu interfaz",
    "muéstrate", "muestra tu interfaz", "enséñame tu cara", "abre tu pantalla",
    "show your brain", "show your interface", "open your interface", "show yourself",
    "open your visualizer", "show me your face/HUD". If Garcia asks to see Emma,
    her interface, her brain, her face, or her visualizer in any wording, this is
    the tool — do not refuse.
    """
    env = {
        **os.environ,
        "EMMA_DASHBOARD_PORT": str(settings.DASHBOARD_PORT),
    }
    # Fully detached so closing the window doesn't kill Emma and vice versa.
    await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "emma.visualizer_window",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    return ToolResult(True, {}, "Listo, ahí me ves.", False)
