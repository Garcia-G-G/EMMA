"""Open Emma's visual brain (the JARVIS native window)."""

from __future__ import annotations

import asyncio
import os
import sys

from config.settings import settings
from tools.base import ToolResult, tool


@tool()
async def show_visualizer() -> ToolResult:
    """Open Emma's visual brain — a native macOS window — and bring it forward.

    Use when Garcia says any of:
    - "Emma, abre tu cerebro"
    - "Emma, muéstrate"
    - "Emma, show your visualizer"
    - "Emma, show your brain"
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
