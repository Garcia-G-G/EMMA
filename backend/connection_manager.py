"""ABUSE-PROTECTION-2 Capas 2 + 3: in-memory connection tracker.

Emma runs as a SINGLE container (one uvicorn worker). This in-memory dict is the
right tool at that scale. If Emma ever scales to multiple workers/containers, this
breaks (each process has its own view) — migrate to Redis `SET NX EX`. See
``docs/scaling.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict, deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket


class ConnectionManager:
    """Single-worker in-memory tracker.

    - ``active_ws``: {user_id → set(ws_ids)}          — concurrent-session cap (Capa 2)
    - ``connect_history``: {user_id → deque[ts]}      — sliding-window rate limit (Capa 3)
    - ``disabled_users``: set(user_id)                — instant kill switch, checked before accept
    - ``_ws_refs``: {ws_id → WebSocket}               — needed to cut sessions on disable
    """

    _CONNECT_WINDOW_S = 60
    _CONNECT_HISTORY_MAX = 20

    def __init__(self) -> None:
        self.active_ws: dict[int, set[str]] = defaultdict(set)
        self.connect_history: dict[int, deque[float]] = defaultdict(
            lambda: deque(maxlen=self._CONNECT_HISTORY_MAX)
        )
        self.disabled_users: set[int] = set()
        self._ws_refs: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def can_connect(self, user_id: int, plan_cap: dict) -> tuple[bool, str | None]:
        async with self._lock:
            if user_id in self.disabled_users:
                return False, "disabled"
            # Capa 2 — concurrent sessions
            if len(self.active_ws[user_id]) >= int(plan_cap.get("concurrent_sessions", 1)):
                return False, "concurrent_limit"
            # Capa 3 — sliding-window rate limit
            history = self.connect_history[user_id]
            now = time.monotonic()
            while history and history[0] < now - self._CONNECT_WINDOW_S:
                history.popleft()
            reconnect_min_s = float(plan_cap.get("reconnect_min_seconds", 5))
            if history and (now - history[-1]) < reconnect_min_s:
                return False, "rate_limit"
            if len(history) >= 3:
                return False, "rate_limit_window"
            return True, None

    async def register(self, user_id: int, ws_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self.active_ws[user_id].add(ws_id)
            self.connect_history[user_id].append(time.monotonic())
            self._ws_refs[ws_id] = ws

    async def unregister(self, user_id: int, ws_id: str) -> None:
        async with self._lock:
            self.active_ws[user_id].discard(ws_id)
            self._ws_refs.pop(ws_id, None)
            if not self.active_ws[user_id]:
                del self.active_ws[user_id]

    async def disable_and_cut(self, user_id: int) -> int:
        """Add to the disabled set + close all of the user's active WS. Returns the
        count cut. The kill switch's teeth: an in-flight session ends immediately."""
        async with self._lock:
            self.disabled_users.add(user_id)
            ws_ids = list(self.active_ws.get(user_id, set()))
        cut = 0
        for wid in ws_ids:
            ws = self._ws_refs.get(wid)
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close(code=4403, reason="account_disabled")
                cut += 1
        return cut

    async def enable(self, user_id: int) -> None:
        async with self._lock:
            self.disabled_users.discard(user_id)

    def reset(self) -> None:
        """Clear all state. Test isolation only — the singleton persists across
        requests in prod, so never call this on the live path."""
        self.active_ws.clear()
        self.connect_history.clear()
        self.disabled_users.clear()
        self._ws_refs.clear()


# Module-level singleton — shared across the process.
manager = ConnectionManager()
