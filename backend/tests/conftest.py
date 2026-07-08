"""Shared backend test fixtures.

The ConnectionManager (ABUSE-PROTECTION-2, Capas 2/3/7) is a process-global
singleton. Tests reuse ``user_id=1`` (each test's fresh DB restarts AUTOINCREMENT),
so without a reset the sliding-window rate limiter carries state between tests and
trips 4429 spuriously. Reset it around every test for isolation.
"""

from __future__ import annotations

import pytest

from backend.connection_manager import manager


@pytest.fixture(autouse=True)
def _reset_connection_manager():
    manager.reset()
    yield
    manager.reset()
