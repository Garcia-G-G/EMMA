"""Operator-only cost/usage overview (account-surface build).

Read-only aggregates of OpenAI spend + sessions. Gated by require_admin
(ADMIN_EMAILS). Revenue/margin needs Stripe queries — deferred (spend side only).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from backend import db
from backend.auth import require_admin
from backend.config import settings

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
