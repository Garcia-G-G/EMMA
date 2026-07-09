"""WebSocket proxy to the OpenAI Realtime API (Prompt 31, A2).

The browser connects to ``/realtime?token=<jwt>``; the backend validates the JWT,
opens its OWN socket to OpenAI with the master key, and relays messages both ways.
The OpenAI key NEVER reaches the client. The 2-min cap is enforced here, server-side
(a timer task), so a tampered client clock can't extend it. Usage is tallied from
OpenAI ``response.done`` events and written to the session row on close.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time

import websockets
from fastapi import APIRouter, WebSocket

from backend import db, metering
from backend.config import PLAN_CAPS, settings
from backend.connection_manager import manager as conn_mgr
from backend.device_pairing import resolve_token
from backend.session import decode_token

log = logging.getLogger("emma.realtime_proxy")
router = APIRouter()


def _is_client_session_frame(raw: str) -> bool:
    """True if a browser frame tries to reconfigure the session (instructions, tools,
    voice, model). Those must be dropped — only our server-side session.update is trusted."""
    try:
        return str(json.loads(raw).get("type", "")).startswith("session.")
    except Exception:
        return False


def cost_usd(tokens_in: int, tokens_out: int) -> float:
    """Estimate session cost from Realtime token usage."""
    return round(
        tokens_in / 1_000_000 * settings.COST_PER_M_INPUT
        + tokens_out / 1_000_000 * settings.COST_PER_M_OUTPUT,
        4,
    )


@router.websocket("/realtime")
async def realtime(ws: WebSocket) -> None:
    """Two clients share this endpoint (two clear paths, no branching merge):

    - **Daemon** (managed voice): presents ``Authorization: Bearer <device token>``;
      routed to :func:`_device_realtime` (resolves the device, enforces the plan
      cap, meters seconds into ``usage_events``).
    - **Web demo**: presents ``?token=<JWT>``; routed to :func:`_demo_realtime`
      (unchanged 60-second flow).
    """
    auth = ws.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        await _device_realtime(ws, auth.split(" ", 1)[1].strip())
    else:
        await _demo_realtime(ws)


async def _device_realtime(ws: WebSocket, token: str) -> None:
    """Managed voice: daemon → backend → OpenAI. Backend holds OUR key and meters.

    The daemon is a trusted first-party client (unlike the browser demo), so its
    ``session.*`` frames ARE forwarded — that's how Pipecat configures voice/tools.
    """
    device = resolve_token(token)
    if not device:
        await ws.close(code=4401, reason="invalid token")
        return
    user_id = device["user_id"]
    user = db.get_user(user_id)
    plan = (user or {}).get("plan", "free")
    cap = PLAN_CAPS.get(plan, PLAN_CAPS["free"])
    now_ts = time.time()

    # CAPA 7 — admin kill switch. DB is the source of truth (survives restart);
    # the in-memory set is the fast path + handles a mid-session disable.
    flags = db.get_user_flags(user_id)
    if (flags and flags["disabled"]) or user_id in conn_mgr.disabled_users:
        await ws.close(code=4403, reason="disabled")
        return

    # CAPA 6 — anomaly throttle window.
    if flags and flags.get("throttle_until") and flags["throttle_until"] > now_ts:
        await ws.close(code=4429, reason="anomaly_throttle")
        return

    # CAPA 4 — daily cap (config existed; enforced here for the first time). Only
    # binds pro/power — free's monthly=0 gate below cuts first.
    daily_cap_s = int(cap.get("daily_seconds", 0) or 0)
    used_today = db.seconds_used_today(user_id) if daily_cap_s > 0 else 0
    if daily_cap_s > 0 and used_today >= daily_cap_s:
        await ws.close(code=4402, reason="daily_cap")
        return

    # CAPA 5 — monthly allowance / balance. Bridges to DASHBOARD-CREDITS once it
    # ships; until then falls back to the raw monthly cap (free=0 → cut here).
    monthly_cap_s = int(cap.get("monthly_seconds", 0) or 0)
    used_month = metering.seconds_used_this_month(user_id)
    available_month = _total_available_seconds(user_id, plan, monthly_cap_s, used_month)
    if available_month <= 0:
        if not await _try_auto_refill(user_id, plan):
            await ws.close(code=4402, reason="balance_zero")
            return
        # Refill topped up the balance — recompute what's now available.
        available_month = _total_available_seconds(
            user_id, plan, monthly_cap_s, metering.seconds_used_this_month(user_id))

    # Bound the per-session ceiling to the budget ACTUALLY remaining. Usage is only
    # billed on close, so without this a single session (or a burst of reconnects)
    # could run a full daemon_session_max past an almost-exhausted daily/monthly cap.
    session_max = int(cap.get("daemon_session_max_seconds", 1800))
    if daily_cap_s > 0:
        session_max = min(session_max, daily_cap_s - used_today)
    if monthly_cap_s > 0:
        session_max = min(session_max, available_month)
    session_max = max(1, session_max)

    # Baseline the user still has this month (free-flow within the monthly cap). Only
    # the overflow beyond this consumes prepaid EXTRA seconds (reserved below).
    baseline_left_s = max(0, monthly_cap_s - used_month)

    # Accept first, then atomically reserve the slot. try_register does the Capa 2/3
    # (concurrent + rate) AND kill-switch check under ONE lock together with the
    # registration, so two simultaneous connects can't both pass a cap of 1, and a
    # connect can't slip past a concurrent disable_and_cut. (A plain check-then-
    # register would be a TOCTOU race across the await points above.)
    await ws.accept()
    ws_id = secrets.token_hex(12)
    ok, why = await conn_mgr.try_register(user_id, ws_id, ws, cap)
    if not ok:
        code = {"disabled": 4403, "concurrent_limit": 4409,
                "rate_limit": 4429, "rate_limit_window": 4429}.get(why or "", 4429)
        await ws.close(code=code, reason=why or "rate_limit")
        return
    started = time.monotonic()
    # DASHBOARD-CREDITS-2: hold the prepaid EXTRA seconds this session might use
    # beyond its baseline, so concurrent sessions can't double-spend the balance.
    # Guarded + lazy — a balance error must never break a session; released/consumed
    # in the finally.
    _reserve_extra(user_id, ws_id, session_max, baseline_left_s)
    # Token usage tallied from OpenAI `response.done` frames (authoritative — audio
    # seconds are only a sanity check). Recorded once on close.
    tally = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "audio_tokens": 0}
    oai_url = f"{settings.OPENAI_REALTIME_URL}?model={settings.OPENAI_REALTIME_MODEL}"
    headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}
    try:
        async with websockets.connect(oai_url, additional_headers=headers, max_size=None) as oai:
            async def client_to_openai() -> None:
                while True:
                    await oai.send(await ws.receive_text())

            async def openai_to_client() -> None:
                async for msg in oai:
                    if isinstance(msg, str):
                        with contextlib.suppress(Exception):
                            ev = json.loads(msg)
                            if ev.get("type") == "response.done":
                                u = (ev.get("response") or {}).get("usage") or {}
                                tally["input_tokens"] += int(u.get("input_tokens", 0))
                                tally["output_tokens"] += int(u.get("output_tokens", 0))
                                itd = u.get("input_token_details") or {}
                                tally["cached_tokens"] += int(itd.get("cached_tokens", 0))
                                tally["audio_tokens"] += int(itd.get("audio_tokens", 0))
                    await ws.send_text(msg if isinstance(msg, str) else msg.decode())

            async def hard_timeout() -> None:
                await asyncio.sleep(session_max)  # server-side session ceiling
                with contextlib.suppress(Exception):
                    await ws.send_text(json.dumps({"type": "emma.session_expired"}))

            # Warning ticker: proactive 80%/90% voice notices injected into THIS
            # upstream session (no separate cost). Runs alongside the pumps; cancelled
            # when the session ends.
            ticker = asyncio.create_task(_warning_ticker(oai, user_id, cap))
            tasks = {asyncio.create_task(t())
                     for t in (client_to_openai, openai_to_client, hard_timeout)}
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                # Drive cancellations to completion before the `async with` closes
                # `oai` out from under a suspended pump, and retrieve the finished
                # task's exception (e.g. the client's WebSocketDisconnect) so it isn't
                # logged as "exception never retrieved".
                await asyncio.gather(*done, *pending, return_exceptions=True)
            finally:
                ticker.cancel()
                # CancelledError is a BaseException, so suppress(Exception) would NOT
                # catch it — the ticker sleeps most of its life, so cancel raises here.
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker
    except Exception:
        pass
    finally:
        seconds = int(min(time.monotonic() - started, float(session_max)))
        with contextlib.suppress(Exception):
            await conn_mgr.unregister(user_id, ws_id)
        # Anomaly scoring reads the PRIOR baseline (before record_usage below adds
        # this session). Kept synchronous: bump_anomaly_score folds the EMA in one
        # atomic statement, so this is now ~2-4 fast local sqlite ops, not the ~7 it
        # was. It logs its own failures rather than being swallowed here. (If the
        # backend ever goes multi-worker / higher-QPS, move to run_in_executor.)
        with contextlib.suppress(Exception):
            _update_anomaly_score(user_id, seconds)
        with contextlib.suppress(Exception):
            metering.record_usage(
                device["user_id"], device["id"], seconds, kind="realtime", model="gpt-realtime",
                input_tokens=tally["input_tokens"], output_tokens=tally["output_tokens"],
                cached_tokens=tally["cached_tokens"], audio_tokens=tally["audio_tokens"])
        # DASHBOARD-CREDITS-2: consume the EXTRA seconds spent beyond baseline (and
        # release the rest of the hold). No-op if no reservation was made.
        _finalize_extra(ws_id, seconds, baseline_left_s)
        with contextlib.suppress(Exception):
            await ws.close()


async def _demo_realtime(ws: WebSocket) -> None:
    token = ws.query_params.get("token", "")
    try:
        claims = decode_token(token)
    except Exception:
        await ws.close(code=4401)  # invalid/expired session token
        return

    if claims.get("kind") not in ("demo", "user"):
        await ws.close(code=4401)
        return

    await ws.accept()
    sid = str(claims["sid"])
    max_seconds = int(claims.get("max_seconds", settings.DEMO_SESSION_SECONDS))
    # Hard $/session ceiling — signed into the token so the client can't widen it.
    cost_cap = int(claims.get("cost_cap_cents") or settings.DEMO_COST_CAP_CENTS) / 100.0
    started = time.time()
    usage = {"in": 0, "out": 0}

    oai_url = f"{settings.OPENAI_REALTIME_URL}?model={settings.OPENAI_REALTIME_MODEL}"
    # GA Realtime API as of 2026-06: NO OpenAI-Beta header. Beta shape was deprecated.
    headers = {"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}

    try:
        async with websockets.connect(oai_url, additional_headers=headers, max_size=None) as oai:
            async def client_to_openai() -> None:
                while True:
                    raw = await ws.receive_text()
                    # NEVER let the browser reconfigure the session (instructions, tools,
                    # voice, model). Only OUR session.update — sent server-side — is trusted.
                    if _is_client_session_frame(raw):
                        continue
                    await oai.send(raw)

            async def openai_to_client() -> None:
                async for msg in oai:
                    with contextlib.suppress(Exception):
                        ev = json.loads(msg)
                        if ev.get("type") == "response.done":
                            u = ev.get("response", {}).get("usage", {})
                            usage["in"] += int(u.get("input_tokens", 0))
                            usage["out"] += int(u.get("output_tokens", 0))
                    await ws.send_text(msg if isinstance(msg, str) else msg.decode())

            async def hard_timeout() -> None:
                # Wake periodically so the cost cap can also end the session early.
                while time.time() - started < max_seconds:
                    if cost_usd(usage["in"], usage["out"]) >= cost_cap:
                        break
                    await asyncio.sleep(1.0)
                with contextlib.suppress(Exception):
                    await ws.send_text(json.dumps({"type": "emma.session_expired"}))

            tasks = {asyncio.create_task(t()) for t in (client_to_openai, openai_to_client, hard_timeout)}
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception:
        pass
    finally:
        seconds = min(time.time() - started, float(max_seconds))
        db.end_session(sid, seconds, usage["in"], usage["out"], cost_usd(usage["in"], usage["out"]))
        with contextlib.suppress(Exception):
            await ws.close()


# ---- ABUSE-PROTECTION-2 helpers ---------------------------------------------
# The balance / auto-refill bridges import DASHBOARD-CREDITS lazily; that module
# doesn't exist yet, so the ImportError fallback keeps this correct today (raw
# monthly cap) and lights up automatically once DASHBOARD-CREDITS ships.


def _total_available_seconds(user_id: int, plan: str, monthly_cap_s: int, used_month: int) -> int:
    """Seconds the user may still spend this month. Delegates to DASHBOARD-CREDITS
    (bundles/balance) when available; else raw monthly cap minus usage."""
    try:
        from backend.dashboard_credits import balance_left_seconds

        return int(balance_left_seconds(user_id, plan, monthly_cap_s, used_month))
    except ImportError:
        return max(0, monthly_cap_s - used_month)


async def _try_auto_refill(user_id: int, plan: str) -> bool:
    """Bridge to the DASHBOARD-CREDITS Stripe auto-refill. Returns True only if the
    user now has minutes. False when opted out, out of Stripe, or the module is absent."""
    try:
        from backend.dashboard_credits import trigger_auto_refill

        return bool(await trigger_auto_refill(user_id, plan))
    except ImportError:
        return False


def _is_auto_refill_enabled(user_id: int) -> bool:
    try:
        from backend.dashboard_credits import is_auto_refill_enabled

        return bool(is_auto_refill_enabled(user_id))
    except ImportError:
        return False


def _reserve_extra(user_id: int, session_id: str, session_max_s: int, baseline_left_s: int) -> None:
    """Hold prepaid EXTRA seconds for this session (DASHBOARD-CREDITS-2). Lazy-import
    + suppress so a missing module or balance error never breaks a session."""
    try:
        from backend.dashboard_credits import reserve_for_session
    except ImportError:
        return
    with contextlib.suppress(Exception):
        reserve_for_session(user_id, session_id, session_max_s, baseline_left_s)


def _finalize_extra(session_id: str, seconds_used: int, baseline_left_s: int) -> None:
    """Consume/release the session's EXTRA reservation on close. Idempotent + guarded."""
    try:
        from backend.dashboard_credits import finalize_session
    except ImportError:
        return
    with contextlib.suppress(Exception):
        finalize_session(session_id, seconds_used, baseline_left_s)


async def _inject_assistant_message(upstream_ws, text: str) -> None:
    """Ask upstream OpenAI to speak ``text`` in the current session — Emma delivers
    the notice in her own voice, no separate cost. Sends to the UPSTREAM websockets
    client (``oai``), which uses ``.send`` (the browser side uses ``send_text``)."""
    if upstream_ws is None:
        return
    frame = {"type": "response.create",
             "response": {"instructions": text, "modalities": ["audio"]}}
    with contextlib.suppress(Exception):
        await upstream_ws.send(json.dumps(frame))


async def _warning_ticker(upstream_ws, user_id: int, cap: dict) -> None:
    """Every 30s, give a proactive voice notice ONCE per threshold per session:
    80% of the daily cap, 90% of the monthly allowance. The ``given`` set prevents
    repeats. Cancelled when the session ends."""
    given: set[str] = set()
    daily_cap = int(cap.get("daily_seconds", 0) or 0)
    monthly_cap = int(cap.get("monthly_seconds", 0) or 0)
    daily_soft_s = daily_cap * 0.8
    monthly_soft_s = monthly_cap * 0.9
    while True:
        await asyncio.sleep(30)
        if daily_soft_s and "day80" not in given:
            u_day = db.seconds_used_today(user_id)
            if u_day >= daily_soft_s:
                remaining_min = max(0, int((daily_cap - u_day) / 60))
                await _inject_assistant_message(
                    upstream_ws,
                    f"Llevas ya un buen rato hoy. Te quedan como {remaining_min} minutos.")
                given.add("day80")
        if monthly_soft_s and "mnth90" not in given:
            u_mnth = metering.seconds_used_this_month(user_id)
            if u_mnth >= monthly_soft_s:
                if _is_auto_refill_enabled(user_id):
                    msg = ("Estás cerca de tu límite mensual. Cuando llegue, "
                           "recargo automáticamente 50 minutos por 9.99 dólares.")
                else:
                    msg = ("Vas al 90% de tu plan del mes. Si quieres que no se corte, "
                           "activa la auto-recarga en tu panel.")
                await _inject_assistant_message(upstream_ws, msg)
                given.add("mnth90")


def _update_anomaly_score(user_id: int, session_seconds: int) -> None:
    """EMA z-score of this session's length vs the last 14. Auto-throttle at 4-sigma
    EMA. Needs >=4 prior sessions to have a baseline (else no-op). Runs in a thread
    executor (blocking sqlite); logs its own failures so a detector error can't be
    silently swallowed by the caller's suppress()."""
    from statistics import mean, stdev

    try:
        baseline = db.recent_session_seconds(user_id, limit=14)
        if len(baseline) < 4:
            return
        m = mean(baseline)
        s = max(stdev(baseline), 1.0)  # >=2 samples guaranteed by the <4 guard above
        z = (session_seconds - m) / s
        # Atomic fold — no read-modify-write race when many sessions close at once.
        ema_new = db.bump_anomaly_score(user_id, z)
        if ema_new > 4.0:
            db.set_user_throttle(user_id, time.time() + 3600)  # 1h
            db.append_status_event(
                user_id, "throttle", actor_id=None,
                reason=f"anomaly z={z:.2f} ema={ema_new:.2f}")
            log.warning("anomaly_throttle user_id=%s z=%.2f ema=%.2f", user_id, z, ema_new)
    except Exception as exc:  # never let scoring break session teardown
        log.warning("anomaly_score_failed user_id=%s error=%s", user_id, exc)
