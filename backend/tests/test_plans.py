"""LANDING-27 — plan structure + plan-gated demo caps.

The binding caps (session length, daily/monthly seconds, cost cap) travel in the
SIGNED JWT so the browser can't inflate them. These tests assert the *issuing* gate:
anonymous → free, and an authed user gets their plan's caps and is throttled at the
daily/monthly ceilings. The demo bypass header skips Turnstile/IP limits for testing.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", tempfile.mktemp(suffix=".db"))

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.app import app
from backend.config import PLAN_CAPS, settings


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "PUBLIC_URL", "http://localhost")
    monkeypatch.setattr(settings, "DEMO_BYPASS_TOKEN", "test-bypass", raising=False)
    db.init_db()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email="a@b.com", plan="free"):
    client.post("/api/auth/register", json={"email": email, "password": "correcthorse9"})
    row = db.get_user_by_email(email)
    if plan != "free":
        db.set_plan(row["id"], plan)
    return row["id"]


# ---- public pricing ---------------------------------------------------------


def test_api_plans_lists_pro_and_power(client):
    body = client.get("/api/plans").json()
    ids = {p["id"] for p in body["plans"]}
    assert ids == {"pro", "power"}
    pro = next(p for p in body["plans"] if p["id"] == "pro")
    assert pro["monthly"] == 19 and pro["session_min"] == 5


def test_plan_caps_have_expected_shape():
    for plan in ("free", "pro", "power"):
        caps = PLAN_CAPS[plan]
        assert {
            "session_seconds",
            "daily_seconds",
            "monthly_seconds",
            "cost_cap_cents",
        } <= caps.keys()
    assert PLAN_CAPS["team"] == PLAN_CAPS["power"]  # team is a legacy alias


def test_free_is_the_90s_trial():
    # PAID-ONBOARDING: free = 90 managed daemon seconds/month, a single session can't
    # exceed the whole budget, and it NEVER auto-charges (hard-stop → app upsells).
    free = PLAN_CAPS["free"]
    assert free["monthly_seconds"] == 90
    assert free["daemon_session_max_seconds"] == 90
    assert free["overage_per_min_usd"] == 0.0


# ---- demo caps per plan -----------------------------------------------------


def _start_demo(client):
    return client.post(
        "/demo/sessions", json={"lang": "es"}, headers={"x-demo-bypass": "test-bypass"}
    )


def test_anonymous_demo_is_free_caps(client):
    # bypass skips Turnstile/IP gating but still yields the anonymous (free) caps
    r = _start_demo(client)
    assert r.status_code == 200
    body = r.json()
    assert body["duration_seconds"] == PLAN_CAPS["free"]["session_seconds"] == 60
    assert body["cost_cap_cents"] == PLAN_CAPS["free"]["cost_cap_cents"]


def test_pro_user_gets_pro_caps(client):
    _register(client, plan="pro")  # cookie set on the client
    r = client.post("/demo/sessions", json={"lang": "es"})
    body = r.json()
    assert body["duration_seconds"] == PLAN_CAPS["pro"]["session_seconds"] == 300
    assert body["cost_cap_cents"] == PLAN_CAPS["pro"]["cost_cap_cents"]


def test_power_user_gets_power_caps(client):
    _register(client, plan="power")
    body = client.post("/demo/sessions", json={"lang": "es"}).json()
    assert body["duration_seconds"] == PLAN_CAPS["power"]["session_seconds"] == 900


def test_daily_cap_blocks_with_429(client):
    uid = _register(client, plan="pro")
    # burn the whole daily allowance via a finished session
    sid = db.create_session(uid)
    db.end_session(sid, PLAN_CAPS["pro"]["daily_seconds"], 0, 0, 0.0)
    r = client.post("/demo/sessions", json={"lang": "es"})
    assert r.status_code == 429


def test_monthly_cap_blocks_with_402_and_overage_url(client):
    uid = _register(client, plan="pro")
    # bump monthly usage to the ceiling without tripping the daily cap (older session)
    import time

    sid = db.create_session(uid)
    db.end_session(sid, PLAN_CAPS["pro"]["monthly_seconds"], 0, 0, 0.0)
    # age the session past 24h so the daily check passes but monthly_seconds_used stays
    conn = db.connect()
    conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (time.time() - 90000, sid))
    conn.commit()
    conn.close()
    r = client.post("/demo/sessions", json={"lang": "es"})
    assert r.status_code == 402
    assert r.json()["detail"]["overage_url"] == "/plans"
