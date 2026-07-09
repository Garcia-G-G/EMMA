"""Operator-only cost/usage overview (account-surface build).

Read-only aggregates of OpenAI spend + sessions. Gated by require_admin
(ADMIN_EMAILS). Revenue/margin needs Stripe queries — deferred (spend side only).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from backend import db
from backend.auth import require_admin
from backend.config import settings
from backend.connection_manager import manager as conn_mgr

router = APIRouter()


@router.get("/api/admin/overview")
async def admin_overview(request: Request) -> dict[str, Any]:
    await require_admin(request)
    stats = db.day_session_stats()  # {"sessions", "cost_usd"} over the last 24h
    return {
        "spend_today_usd": round(db.day_cost_usd(), 2),
        "spend_month_usd": round(db.month_cost_usd(), 2),
        "sessions_today": stats["sessions"],
        "monthly_budget_usd": settings.MONTHLY_BUDGET_USD,
        "demo_daily_ceiling_usd": settings.DEMO_DAILY_USD_CEILING,
    }


# ---- ABUSE-PROTECTION-2 Capa 7: admin kill switch ---------------------------


async def _json_body(request: Request) -> dict[str, Any]:
    """Parse a JSON body, returning a clean 400 (not a 500 stack trace) on garbage."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON object expected")
    return body


@router.post("/api/admin/disable-user")
async def admin_disable_user(request: Request) -> dict[str, Any]:
    """Disable a user: flag them, revoke every device token, cut live sessions, and
    append a hash-chained audit row. Idempotent."""
    admin_user = await require_admin(request)
    body = await _json_body(request)
    email = (body.get("email") or "").strip().lower()
    reason = (body.get("reason") or "").strip()
    if not email:
        raise HTTPException(400, "email required")

    user = db.get_user_by_email(email)
    if not user:
        raise HTTPException(404, "user not found")

    # In-memory kill FIRST (populates disabled_users + cuts live WS), then persist —
    # so a connect racing this can't slip past the in-memory gate before it's set.
    cut = await conn_mgr.disable_and_cut(user["id"])
    db.set_user_disabled(user["id"], reason)
    devices_revoked = db.revoke_all_device_tokens_for_user(user["id"])
    db.append_status_event(
        user["id"], "disable", actor_id=admin_user["id"], reason=reason,
        ip=request.client.host if request.client else None,
        ua=request.headers.get("user-agent", "")[:200],
    )
    return {"ok": True, "sessions_cut": cut, "devices_revoked": devices_revoked,
            "user_id": user["id"]}


@router.post("/api/admin/enable-user")
async def admin_enable_user(request: Request) -> dict[str, Any]:
    """Re-enable a previously disabled user + append an audit row. Idempotent."""
    admin_user = await require_admin(request)
    body = await _json_body(request)
    email = (body.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "email required")

    user = db.get_user_by_email(email)
    if not user:
        raise HTTPException(404, "user not found")

    db.clear_user_disabled(user["id"])
    db.append_status_event(
        user["id"], "enable", actor_id=admin_user["id"],
        reason=(body.get("reason") or "reactivated by admin"),
    )
    await conn_mgr.enable(user["id"])
    return {"ok": True, "user_id": user["id"]}


@router.get("/api/admin/status-events")
async def admin_status_events(request: Request, user_id: int) -> dict[str, Any]:
    await require_admin(request)
    return {"events": db.status_events_for_user(user_id, limit=50)}
