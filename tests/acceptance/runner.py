"""Acceptance suite runner.

Modes:
- ``--mock-external`` (CI smoke). Synthesizes a passing TurnResult per
  scenario from ``expected_actions`` + ``mock_spoken_text``. Verifies
  that the YAML loads, the runner machinery works, and the report
  renders. Exits 0 only if every scenario passes.
- ``--voice`` (19.7). ElevenLabs-synthesized audio played at a REAL Emma
  subprocess through a virtual cable (or the speaker→mic fallback);
  events collected from her structlog JSON stream. See
  ``VOICE_HARNESS_README.md``.
- live (default). Drives ``core.llm.converse`` with the scenario's
  utterance, captures real tool calls via a monkey-patched
  ``tools.registry.dispatch``, and matches the assistant's accumulated
  text against ``expected_spoken_pattern``. (Disabled post-Prompt-13;
  voice mode is the live path now.)

Live/voice modes require the full Emma runtime (uv sync) and real API
keys in ``.env``. Several scenarios are marked ``live_blocked_by`` so
the runner reports them as SKIP with a clear reason rather than FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCENARIOS_PATH = Path(__file__).parent / "scenarios.yaml"
TEMPLATE_PATH = Path(__file__).parent / "report_template.md"
REPORT_DIR = Path(__file__).parent


# ---------- data shapes -------------------------------------------------


@dataclass
class ToolCallRecord:
    name: str
    args: dict[str, Any]
    success: bool | None = None
    user_message: str | None = None


@dataclass
class TurnResult:
    spoken_text: str = ""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None
    pending_confirmation: Any = None


@dataclass
class ScenarioOutcome:
    scenario: dict[str, Any]
    result: TurnResult
    passed: bool
    issues: list[str]
    status: str  # PASS | FAIL | SKIP

    @property
    def id(self) -> str:
        return self.scenario["id"]


# ---------- scenario loader --------------------------------------------


def load_scenarios() -> list[dict[str, Any]]:
    text = SCENARIOS_PATH.read_text()
    # Strip any leading `#` YAML comments so json.loads can parse the
    # JSON-formatted body.
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    body = "\n".join(lines).strip()
    return json.loads(body)


# ---------- assertions --------------------------------------------------


def _args_contain(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for k, v in expected.items():
        if k not in actual:
            return False
        if isinstance(v, str) and isinstance(actual[k], str):
            if v.lower() not in actual[k].lower():
                return False
        elif actual[k] != v:
            return False
    return True


def check_scenario(scenario: dict[str, Any], result: TurnResult) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if result.error:
        issues.append(f"error: {result.error}")

    expected_actions = scenario.get("expected_actions", [])
    for ea in expected_actions:
        wanted_tool = ea["tool"]
        wanted_args = ea.get("args_contain", {})
        match = next(
            (
                c
                for c in result.tool_calls
                if c.name == wanted_tool and _args_contain(c.args, wanted_args)
            ),
            None,
        )
        if match is None:
            issues.append(
                f"missing tool call: {wanted_tool}"
                + (f" with args containing {wanted_args}" if wanted_args else "")
            )

    pattern = scenario.get("expected_spoken_pattern")
    if pattern and not re.search(pattern, result.spoken_text, re.IGNORECASE | re.DOTALL):
        issues.append(f"spoken text did not match /{pattern}/i")

    budget = scenario.get("max_latency_ms")
    if budget and result.latency_ms > budget:
        issues.append(f"latency {result.latency_ms}ms exceeded budget {budget}ms")

    return (not issues), issues


# ---------- mock mode ---------------------------------------------------


def synthesize_mock_turn(scenario: dict[str, Any]) -> TurnResult:
    """Build a passing TurnResult by copying expected_actions + mock text."""
    calls = [
        ToolCallRecord(
            name=ea["tool"],
            args=dict(ea.get("args_contain", {})),
            success=True,
            user_message=scenario.get("mock_spoken_text", ""),
        )
        for ea in scenario.get("expected_actions", [])
    ]
    return TurnResult(
        spoken_text=scenario.get("mock_spoken_text", ""),
        tool_calls=calls,
        latency_ms=10,
    )


async def run_mock(scenario: dict[str, Any]) -> TurnResult:
    primary = synthesize_mock_turn(scenario)
    followup = scenario.get("followup")
    if followup:
        secondary = synthesize_mock_turn(followup)
        primary.spoken_text += "\n" + secondary.spoken_text
        primary.tool_calls.extend(secondary.tool_calls)
        primary.latency_ms += secondary.latency_ms
    return primary


# ---------- live mode ---------------------------------------------------


class _LiveSession:
    """Drives a real ``converse()`` run with tool-call capture.

    Constructed lazily so importing this module doesn't pull in the
    heavy Emma runtime when only mock mode is needed.
    """

    def __init__(self) -> None:
        # Post Prompt-13 (Pipecat) migration: core.llm / core.stt are
        # gone. Live mode is disabled and raises NotImplementedError
        # below; we only keep the constructor surface so the class can
        # still be instantiated for compat with old import paths.
        from core import runtime
        from tools import registry

        self._runtime = runtime
        self._registry = registry
        self._original_dispatch = registry.dispatch
        self.history: list[Any] = []
        self.recorded: list[ToolCallRecord] = []
        self.last_pending: list[Any] = []

    def __enter__(self) -> _LiveSession:
        original = self._original_dispatch
        recorded = self.recorded

        async def wrapped(name: str, args: dict[str, Any]) -> Any:
            rec = ToolCallRecord(name=name, args=dict(args))
            try:
                result = await original(name, dict(args))
            except Exception as exc:
                rec.success = False
                rec.user_message = f"<dispatch raised: {exc}>"
                recorded.append(rec)
                raise
            rec.success = bool(getattr(result, "success", False))
            rec.user_message = getattr(result, "user_message", "")
            recorded.append(rec)
            return result

        self._registry.dispatch = wrapped
        # core.llm no longer exists post Prompt-13 migration; the old
        # patch of `core.llm.dispatch` was for the now-removed converse()
        # tool loop. Pipecat's OpenAIRealtimeLLMService routes function
        # calls through its own registered handlers — the runner has no
        # equivalent patch surface today (live mode is disabled).
        return self

    def __exit__(self, *exc: Any) -> None:
        self._registry.dispatch = self._original_dispatch

    async def utter(self, text: str, language: str) -> TurnResult:
        spoken_lang = language if language in ("es", "en") else "es"
        self._runtime.set_spoken_lang(spoken_lang)  # type: ignore[arg-type]

        # Prompt 13 retired ``core.llm.converse`` in favor of a Realtime
        # WebSocket session. The acceptance runner's live mode used to
        # drive that function directly with a synthetic Transcript; a
        # Realtime-aware live harness is its own piece of work and
        # belongs in a later prompt. Mock mode still exercises the
        # scenarios + runner machinery end-to-end and is the CI path.
        raise NotImplementedError(
            "Live acceptance mode is disabled post Realtime-API migration "
            "(Prompt 13). Run with --mock-external. A Realtime-aware live "
            "harness will land in a follow-up prompt."
        )


async def run_live(
    scenario: dict[str, Any],
    session: _LiveSession | None = None,
) -> TurnResult:
    """Execute a scenario against the real runtime. Builds a session if
    none provided; for ``follows`` chains, callers supply the session.
    """
    if session is None:
        # Caller mismanaged - we lose follow-up state.
        # Fall back to a fresh session to keep the runner moving.
        with _LiveSession() as s:
            return await _live_in(s, scenario)
    return await _live_in(session, scenario)


async def _live_in(session: _LiveSession, scenario: dict[str, Any]) -> TurnResult:
    # If the scenario continues a previous one and there's a pending
    # confirmation in the session, re-dispatch the pending tool with
    # confirmed=True (simulating the user's "sí"). This matches the
    # orchestrator's _handle_confirmation behavior.
    if scenario.get("follows") and session.last_pending:
        pending = session.last_pending.pop(0)
        from tools.registry import dispatch as raw_dispatch

        start = time.monotonic()
        try:
            result = await raw_dispatch(pending.tool_name, {**pending.args, "confirmed": True})
        except Exception as exc:
            return TurnResult(
                spoken_text="",
                tool_calls=[],
                latency_ms=int((time.monotonic() - start) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        latency = int((time.monotonic() - start) * 1000)
        rec = ToolCallRecord(
            name=pending.tool_name,
            args=dict(pending.args),
            success=bool(result.success),
            user_message=result.user_message,
        )
        return TurnResult(
            spoken_text=result.user_message,
            tool_calls=[rec],
            latency_ms=latency,
        )

    primary = await session.utter(scenario["utterance"], scenario.get("language", "es"))
    followup = scenario.get("followup")
    if followup:
        secondary = await session.utter(followup["utterance"], followup.get("language", "es"))
        primary.spoken_text += "\n" + secondary.spoken_text
        primary.tool_calls.extend(secondary.tool_calls)
        primary.latency_ms += secondary.latency_ms
    return primary


# ---------- live phase / environment probes -----------------------------


def _phase_available(n: int) -> bool:
    """Rough check that a phase's surface exists. Conservative."""
    if n == 3:
        try:
            from tools.registry import get_tool

            return get_tool("remember_fact") is not None
        except Exception:
            return False
    return True


def _should_skip_live(scenario: dict[str, Any]) -> str | None:
    needed_phase = scenario.get("expects_phase")
    if needed_phase and not _phase_available(int(needed_phase)):
        return f"phase {needed_phase} not available"
    blocked = scenario.get("live_blocked_by")
    if blocked:
        return blocked
    return None


# ---------- report ------------------------------------------------------


def _summary_row(o: ScenarioOutcome) -> str:
    icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "·"}.get(o.status, "?")
    return f"| {o.id} | {o.scenario['name']} | {icon} {o.status} | {o.result.latency_ms} ms |"


def _detail_block(o: ScenarioOutcome) -> str:
    s = o.scenario
    lines: list[str] = []
    lines.append(f"### {o.id} — {s['name']} ({o.status})")
    lines.append("")
    lines.append(f"- Utterance: `{s['utterance']}`")
    lines.append(f"- Given: {s.get('given', '<unspecified>')}")
    lines.append(f"- Latency: {o.result.latency_ms} ms")
    if o.result.tool_calls:
        lines.append("- Tool calls:")
        for c in o.result.tool_calls:
            lines.append(f"  - `{c.name}({json.dumps(c.args, ensure_ascii=False)})`")
    else:
        lines.append("- Tool calls: none")
    spoken = (o.result.spoken_text or "").strip()
    lines.append(f"- Spoken: {spoken or '<empty>'}")
    if o.result.error:
        lines.append(f"- Error: `{o.result.error}`")
    if o.status == "SKIP":
        reason = s.get("live_blocked_by") or "skipped"
        lines.append(f"- Skip reason: {reason}")
    if o.issues:
        lines.append("- Issues:")
        for issue in o.issues:
            lines.append(f"  - {issue}")
        if o.status == "FAIL":
            lines.append(f"  - Recommended fix: see TODO(v1-blocker) for {o.id}.")
    lines.append("")
    return "\n".join(lines)


def render_report(mode: str, outcomes: list[ScenarioOutcome]) -> str:
    template = TEMPLATE_PATH.read_text()
    summary_rows = "\n".join(_summary_row(o) for o in outcomes)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    counts_md = "\n".join(f"| {k} | {v} |" for k, v in counts.items())
    details_md = "\n".join(_detail_block(o) for o in outcomes)
    return (
        template.replace("{{MODE}}", mode)
        .replace("{{TIMESTAMP}}", dt.datetime.now().isoformat(timespec="seconds"))
        .replace("{{COUNTS}}", counts_md)
        .replace("{{SUMMARY_ROWS}}", summary_rows)
        .replace("{{DETAILS}}", details_md)
    )


# ---------- voice mode (19.7-VAH3) ---------------------------------------


def _run_all_voice(
    scenarios: list[dict[str, Any]], input_device: str, output_device: str
) -> tuple[list[ScenarioOutcome], dict[str, Any], int]:
    """Sequential voice run. Returns (outcomes, extras_by_id, chars_synthesized).

    ``follows`` scenarios reuse the previous scenario's live subprocess so
    pending confirmations survive; everything else gets a fresh daemon.
    Never parallel — the virtual cable / mic is a singleton resource.
    """
    from tests.acceptance import voice_driver

    outcomes: list[ScenarioOutcome] = []
    extras_by_id: dict[str, Any] = {}
    chars_synth = 0
    open_proc: Any = None
    open_id: str | None = None

    def _close_open() -> None:
        nonlocal open_proc, open_id
        if open_proc is not None:
            open_proc.stop()
            open_proc = None
            open_id = None

    for s in scenarios:
        skip = _should_skip_live(s)
        if skip:
            outcomes.append(ScenarioOutcome(s, TurnResult(), False, [skip], "SKIP"))
            continue

        proc = None
        if s.get("follows") and s["follows"] == open_id and open_proc is not None:
            proc = open_proc  # continue the live session
        else:
            _close_open()

        try:
            summary, extras, proc = voice_driver.run_voice_scenario(
                s, input_device=input_device, output_device=output_device, proc=proc
            )
        except Exception as exc:  # harness bug or environment failure
            _close_open()
            result = TurnResult(error=f"{type(exc).__name__}: {exc}")
            outcomes.append(ScenarioOutcome(s, result, False, [str(exc)], "ERROR"))
            continue

        open_proc, open_id = proc, s["id"]
        chars_synth += extras.chars_synthesized
        extras_by_id[s["id"]] = extras

        result = TurnResult(
            spoken_text=summary.get("spoken_text", ""),
            tool_calls=[
                ToolCallRecord(
                    name=c["name"], args=c["args"], success=c["success"], user_message=None
                )
                for c in summary.get("tool_calls", [])
            ],
            latency_ms=int(summary.get("turn_ms", 0)),
            error=extras.error or None,
        )
        # Text-mode latency budgets (direct dispatch) can't apply to a voice
        # turn (wake + VAD + model speech dominate). Voice budgets are opt-in
        # via max_latency_ms_voice; turn time is always REPORTED either way.
        s_check = {k: v for k, v in s.items() if k != "max_latency_ms"}
        if "max_latency_ms_voice" in s:
            s_check["max_latency_ms"] = s["max_latency_ms_voice"]
        passed, issues = check_scenario(s_check, result)
        issues += voice_driver.check_voice_extras(s, extras)
        if extras.error and "daemon never reached" in extras.error:
            status = "ERROR"
        else:
            status = "PASS" if passed and not issues else "FAIL"
        outcomes.append(ScenarioOutcome(s, result, status == "PASS", issues, status))

    _close_open()
    return outcomes, extras_by_id, chars_synth


def render_voice_report(
    outcomes: list[ScenarioOutcome], extras_by_id: dict[str, Any], chars_synth: int
) -> str:
    from tests.acceptance.audio_gen import USD_PER_CHAR

    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    cost = chars_synth * USD_PER_CHAR
    lines = [
        "# Emma voice acceptance run",
        "",
        f"- Timestamp: {dt.datetime.now().isoformat(timespec='seconds')}",
        "- Mode: voice (ElevenLabs → Emma subprocess)",
        "",
        "| passed | failed | errored | skipped | total | cost this run |",
        "|---|---|---|---|---|---|",
        f"| {counts['PASS']} | {counts['FAIL']} | {counts['ERROR']} | {counts['SKIP']} "
        f"| {len(outcomes)} | ${cost:.3f} ({chars_synth} fresh chars @ "
        f"$0.11/1k, Flash v2.5 = 0.5 credits/char) |",
        "",
        "## Scenarios",
        "",
    ]
    for o in outcomes:
        x = extras_by_id.get(o.id)
        lines.append(f"### {o.id} — {o.scenario['name']} ({o.status})")
        lines.append("")
        lines.append(f"- Utterance: `{o.scenario['utterance']}`")
        if x is not None:
            lines.append(f"- Audio: `{x.audio_path}`")
            wake = f"yes ({x.wake_latency_ms} ms after playback start)" if x.wake_detected else "NO"
            lines.append(f"- Wake detected: {wake}")
            stt = x.transcript or "<none>"
            lines.append(f"- STT heard: {stt}")
            if x.transcript and x.transcript.strip().lower() not in o.scenario[
                "utterance"
            ].strip().lower().replace("hey emma, ", ""):
                lines.append(f"  - (input was: `{o.scenario['utterance']}`)")
            if x.capability_gaps:
                lines.append(
                    f"- Capability gaps: {json.dumps(x.capability_gaps, ensure_ascii=False)}"
                )
            if x.exit_code is not None:
                lines.append(f"- Subprocess exit code: {x.exit_code}")
        if o.result.tool_calls:
            lines.append("- Tool calls:")
            for c in o.result.tool_calls:
                ok = "✓" if c.success else "✗"
                lines.append(f"  - {ok} `{c.name}({json.dumps(c.args, ensure_ascii=False)})`")
        else:
            lines.append("- Tool calls: none")
        lines.append(f"- Spoken: {(o.result.spoken_text or '<empty>').strip()}")
        lines.append(f"- Turn time: {o.result.latency_ms} ms")
        if o.issues:
            lines.append("- Issues:")
            lines.extend(f"  - {i}" for i in o.issues)
        lines.append("")
    return "\n".join(lines)


def _confirm_cost_or_exit(skip_prewarm: bool, assume_yes: bool) -> None:
    """Cost guard (19.7-VAH5.2): estimate re-synthesis before any HTTP."""
    from tests.acceptance.audio_gen import USD_PER_CHAR, estimate_missing

    n, chars = estimate_missing()
    cost = chars * USD_PER_CHAR
    if n:
        print(f"cache miss: {n} clips, {chars} chars ≈ ${cost:.2f} (Flash v2.5)")
    if cost > 1.0 and not assume_yes:
        answer = input(f"Re-synthesis will cost ≈ ${cost:.2f}. Continue? [y/N] ")
        if answer.strip().lower() not in ("y", "yes", "s", "sí", "si"):
            raise SystemExit("aborted by cost guard")
    if not skip_prewarm and n:
        from tests.acceptance.audio_gen import prewarm

        generated, reused = prewarm()
        print(f"prewarm: generated {generated}, reused {reused}")


# ---------- orchestration ----------------------------------------------


async def _run_all_mock(scenarios: list[dict[str, Any]]) -> list[ScenarioOutcome]:
    outcomes: list[ScenarioOutcome] = []
    for s in scenarios:
        result = await run_mock(s)
        passed, issues = check_scenario(s, result)
        status = "PASS" if passed else "FAIL"
        outcomes.append(ScenarioOutcome(s, result, passed, issues, status))
    return outcomes


async def _run_all_live(scenarios: list[dict[str, Any]]) -> list[ScenarioOutcome]:
    outcomes: list[ScenarioOutcome] = []
    with _LiveSession() as session:
        for s in scenarios:
            skip = _should_skip_live(s)
            if skip:
                outcomes.append(ScenarioOutcome(s, TurnResult(), False, [skip], "SKIP"))
                continue
            try:
                result = await _live_in(session, s)
            except Exception as exc:
                result = TurnResult(error=f"{type(exc).__name__}: {exc}")
            passed, issues = check_scenario(s, result)
            status = "PASS" if passed else "FAIL"
            outcomes.append(ScenarioOutcome(s, result, passed, issues, status))
    return outcomes


def _exit_code(mode: str, outcomes: list[ScenarioOutcome]) -> int:
    if mode == "mock":
        return 0 if all(o.status == "PASS" for o in outcomes) else 1
    # Live: SKIPs are tolerated; FAILs aren't.
    return 0 if not any(o.status == "FAIL" for o in outcomes) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Emma acceptance suite")
    parser.add_argument(
        "--mock-external",
        action="store_true",
        help="run in mock mode (CI smoke). Bypasses real APIs.",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="voice mode (19.7): play ElevenLabs audio at a real Emma subprocess.",
    )
    parser.add_argument("--filter", default="", help="id glob, e.g. 'A0*' or 'V07'")
    parser.add_argument(
        "--input-device",
        default=None,
        help="Emma's input device substring (default: settings.EMMA_TEST_INPUT_DEVICE; "
        "empty = system default mic, i.e. speaker→mic fallback)",
    )
    parser.add_argument(
        "--output-device",
        default=None,
        help="harness playback device substring (default: settings.EMMA_TEST_OUTPUT_DEVICE; "
        "empty = default speakers)",
    )
    parser.add_argument("--skip-prewarm", action="store_true", help="don't pre-generate audio")
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=1,
        help="must stay 1 — Emma's audio pipeline is a singleton resource",
    )
    parser.add_argument("--yes", action="store_true", help="skip the cost-guard confirmation")
    parser.add_argument(
        "--output",
        default=None,
        help="path to write the report (default: tests/acceptance/run_<timestamp>.md)",
    )
    args = parser.parse_args()

    if args.max_parallel != 1:
        parser.error("--max-parallel must be 1: the mic/virtual-cable is a singleton")

    scenarios = load_scenarios()
    if args.filter:
        import fnmatch

        scenarios = [s for s in scenarios if fnmatch.fnmatch(s["id"], args.filter)]
        if not scenarios:
            parser.error(f"no scenario id matches {args.filter!r}")

    if args.voice:
        from config.settings import settings

        in_dev = (
            args.input_device if args.input_device is not None else settings.EMMA_TEST_INPUT_DEVICE
        )
        out_dev = (
            args.output_device
            if args.output_device is not None
            else settings.EMMA_TEST_OUTPUT_DEVICE
        )
        _confirm_cost_or_exit(args.skip_prewarm, args.yes)
        outcomes, extras_by_id, chars = _run_all_voice(scenarios, in_dev, out_dev)
        out_path = (
            Path(args.output)
            if args.output
            else REPORT_DIR / f"voice_run_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        )
        out_path.write_text(render_voice_report(outcomes, extras_by_id, chars))
        counts = {"PASS": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
        for o in outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        print(
            f"[voice] {counts['PASS']} passed, {counts['FAIL']} failed, "
            f"{counts['ERROR']} errored, {counts['SKIP']} skipped. Report: {out_path}"
        )
        return 0 if not (counts["FAIL"] or counts["ERROR"]) else 1

    mode = "mock" if args.mock_external else "live"
    outcomes = asyncio.run(_run_all_mock(scenarios) if mode == "mock" else _run_all_live(scenarios))

    out_path = (
        Path(args.output)
        if args.output
        else REPORT_DIR / f"run_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    )
    out_path.write_text(render_report(mode, outcomes))

    # Console summary
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    print(
        f"[{mode}] {counts['PASS']} passed, {counts['FAIL']} failed, "
        f"{counts['SKIP']} skipped. Report: {out_path}"
    )
    return _exit_code(mode, outcomes)


if __name__ == "__main__":
    raise SystemExit(main())
