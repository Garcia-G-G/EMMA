"""Shared dataclasses for the proactive engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Priority(IntEnum):
    SILENT = 0  # log only (debug)
    AMBIENT = 1  # visualizer ticker only
    NOTIFY = 2  # macOS notification, no voice
    SPEAK = 3  # voice through synthetic Pipecat session
    URGENT = 4  # voice + notification + visualizer (always wakes you up)


@dataclass
class ProactiveEvent:
    source: str  # "morning_briefing", "meeting_prep", etc.
    priority: Priority
    summary_es: str  # one-line Spanish summary (notification body)
    summary_en: str = ""  # optional English (only when Garcia is in EN context)
    detail: str = ""  # longer text Emma will speak; LLM may rephrase
    meta: dict[str, Any] = field(default_factory=dict)
