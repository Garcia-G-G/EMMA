"""Shared dev-mode state.

The dev tool sets ``shutdown_requested`` after opening the workshop. The
orchestrator polls between turns and exits cleanly (exit 0) so launchd's
``KeepAlive.SuccessfulExit = false`` policy leaves Emma stopped until
the user manually resumes via ``launchctl enable && kickstart``.
"""
from __future__ import annotations

import asyncio

shutdown_requested = asyncio.Event()
