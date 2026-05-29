"""Background task registry + executor.

Tasks are async coroutines wrapped in asyncio.Task. The registry persists
minimal state (id, name, started_at, status, exit_code, last 8KB of output)
to ~/.emma/tasks.jsonl so the user can ask "what did Emma do yesterday?"
and Emma can answer from the log even after a daemon restart.

NOTE: the asyncio.Task itself doesn't survive a daemon restart — only its
log entry does. On restart, in-flight tasks are marked "aborted" in the
log so the user knows they didn't complete.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

from core import events_bus

log = structlog.get_logger("emma.background")

_TASKS_DB = Path.home() / ".emma" / "tasks.jsonl"
_OUTPUT_BUFFER_BYTES = 8192
MAX_PARALLEL_TASKS = 8


Status = Literal["pending", "running", "completed", "failed", "cancelled", "aborted"]


@dataclass
class TaskRecord:
    id: str
    name: str
    kind: str  # e.g. "claude_code", "shell", "python"
    started_at: float
    ended_at: float | None = None
    status: Status = "pending"
    exit_code: int | None = None
    last_output: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class _Registry:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db = db_path or _TASKS_DB
        self._tasks: dict[str, TaskRecord] = {}
        self._handles: dict[str, asyncio.Task[Any]] = {}
        self._output_bufs: dict[str, deque[str]] = {}
        self._db.parent.mkdir(parents=True, exist_ok=True)
        self._load_persisted()

    def _load_persisted(self) -> None:
        if not self._db.exists():
            return
        for line in self._db.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                rec = TaskRecord(**d)
                # in-flight tasks from a previous run become "aborted"
                if rec.status in ("pending", "running"):
                    rec.status = "aborted"
                    rec.error = "daemon restarted while task was running"
                self._tasks[rec.id] = rec
            except Exception as exc:
                log.warning("task_log_parse_failed", error=str(exc))

    def _persist(self, rec: TaskRecord) -> None:
        try:
            line = json.dumps(asdict(rec), ensure_ascii=False)
            with self._db.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            log.error("task_persist_failed", error=str(exc), task=rec.id)

    def active_count(self) -> int:
        """Number of currently-running task handles."""
        return sum(1 for t in self._handles.values() if not t.done())

    def at_capacity(self) -> bool:
        return self.active_count() >= MAX_PARALLEL_TASKS

    def list(self, status: Status | None = None, limit: int = 25) -> list[TaskRecord]:
        items = sorted(self._tasks.values(), key=lambda r: r.started_at, reverse=True)
        if status:
            items = [r for r in items if r.status == status]
        return items[:limit]

    def get(self, name_or_id: str) -> TaskRecord | None:
        if name_or_id in self._tasks:
            return self._tasks[name_or_id]
        for r in self._tasks.values():
            if r.name == name_or_id:
                return r
        return None

    def append_output(self, task_id: str, chunk: str) -> None:
        buf = self._output_bufs.setdefault(task_id, deque(maxlen=120))
        buf.append(chunk)
        rec = self._tasks.get(task_id)
        if rec is not None:
            rec.last_output = "".join(buf)[-_OUTPUT_BUFFER_BYTES:]

    async def start(
        self,
        name: str,
        kind: str,
        coro_factory: Callable[[_TaskController], Awaitable[int]],
        meta: dict[str, Any] | None = None,
    ) -> TaskRecord:
        task_id = uuid.uuid4().hex[:12]
        rec = TaskRecord(
            id=task_id,
            name=name,
            kind=kind,
            started_at=time.time(),
            status="running",
            meta=meta or {},
        )
        self._tasks[task_id] = rec
        self._persist(rec)
        events_bus.publish("task_started", id=task_id, name=name, kind=kind)

        controller = _TaskController(self, task_id)

        async def _wrapper() -> None:
            try:
                exit_code = await coro_factory(controller)
                rec.status = "completed" if exit_code == 0 else "failed"
                rec.exit_code = exit_code
            except asyncio.CancelledError:
                rec.status = "cancelled"
                raise
            except Exception as exc:
                rec.status = "failed"
                rec.error = str(exc)
                log.error("task_crashed", id=task_id, name=name, error=str(exc))
            finally:
                rec.ended_at = time.time()
                self._persist(rec)
                events_bus.publish(
                    "task_completed",
                    id=task_id,
                    name=name,
                    kind=kind,
                    status=rec.status,
                    elapsed_s=int(rec.ended_at - rec.started_at),
                )
                await _notify_macos(name, rec.status, rec.error or rec.last_output[-180:])

        self._handles[task_id] = asyncio.create_task(_wrapper(), name=f"emma-bg-{name}")
        return rec

    async def cancel(self, name_or_id: str) -> bool:
        rec = self.get(name_or_id)
        if rec is None:
            return False
        task = self._handles.get(rec.id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def cancel_all(self) -> None:
        """Cancel every in-flight task (clean-shutdown path)."""
        for task in list(self._handles.values()):
            if not task.done():
                task.cancel()

    async def wait(self, name_or_id: str, timeout_s: float | None = None) -> TaskRecord | None:
        rec = self.get(name_or_id)
        if rec is None:
            return None
        task = self._handles.get(rec.id)
        if task is None or task.done():
            return rec
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        return self._tasks[rec.id]


class _TaskController:
    """Passed to each coroutine factory so the work can append output."""

    def __init__(self, registry: _Registry, task_id: str) -> None:
        self._registry = registry
        self.task_id = task_id

    def append_output(self, chunk: str) -> None:
        self._registry.append_output(self.task_id, chunk)


async def _notify_macos(name: str, status: str, summary: str) -> None:
    """Fire a macOS notification via osascript. Best-effort; ignore failures.

    The body is run through core.redaction.redact (15.7) so a subprocess that
    echoed a credential never lands in a Notification Center banner.
    """
    from core.redaction import redact

    title = "Emma"
    subtitle = f"{name} · {status}"
    body = redact((summary or "")[:200]).replace('"', "'")
    script = f'display notification "{body}" with title "{title}" subtitle "{subtitle}"'
    try:
        from actions import macos

        await macos.osascript(script, timeout_s=4.0)
    except Exception as exc:
        log.warning("notify_failed", error=str(exc))


_registry = _Registry()


def registry() -> _Registry:
    return _registry
