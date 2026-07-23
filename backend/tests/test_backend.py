"""Prompt 31 — backend tests (FastAPI TestClient; OAuth/Stripe/captcha mocked).

Run: ``.venv/bin/python -m pytest backend/tests -q`` (separate from the daemon suite).
Dev mode (no captcha/OAuth/Stripe secrets) lets us exercise the full session gate,
auth cookie, Stripe webhook handling, and dashboard gating offline.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", tempfile.mktemp(suffix=".db"))

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend import auth, db, stripe_routes
from backend.app import app
from backend.config import settings
from backend.realtime_proxy import cost_usd
from backend.session import decode_token, issue_token


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "t.db"))
    db.init_db()
    yield


@pytest.fixture
def client():
    return TestClient(app)


# ---- A: session gate --------------------------------------------------------


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_demo_session_start_then_ip_rate_limit(client):
    r1 = client.post("/api/session/start", json={"captcha_token": ""})
    assert r1.status_code == 200
    body = r1.json()
    assert body["max_seconds"] == settings.DEMO_SESSION_SECONDS and body["session_token"]
    # second demo from the same IP within 24h is blocked
    r2 = client.post("/api/session/start", json={"captcha_token": ""})
    assert r2.status_code == 429


def test_budget_guard_returns_503(client, monkeypatch):
    monkeypatch.setattr(settings, "MONTHLY_BUDGET_USD", 0.0)
    r = client.post("/api/session/start", json={"captcha_token": ""})
    assert r.status_code == 503 and "descansando" in r.json()["detail"]


def test_jwt_roundtrip_and_tamper():
    tok = issue_token("sid1", "demo", 120, None)
    claims = decode_token(tok)
    assert claims["sid"] == "sid1" and claims["max_seconds"] == 120
    with pytest.raises(jwt.PyJWTError):
        decode_token(tok + "x")


def test_cost_accounting():
    assert cost_usd(1_000_000, 0) == settings.COST_PER_M_INPUT
    assert cost_usd(0, 1_000_000) == settings.COST_PER_M_OUTPUT


def test_realtime_rejects_bad_token(client):
    # server closes the socket (code 4401) before accept → client raises
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/realtime?token=garbage"):
        pass


# ---- B: auth ----------------------------------------------------------------


def _login(client, email="alex@example.com", provider="google"):
    user = db.upsert_user(email, "the user", provider, "pid-1")
    client.cookies.set("emma_session", auth._serializer.dumps({"uid": user["id"]}))
    return user


def test_me_401_when_anon(client):
    assert client.get("/api/me").status_code == 401


def test_login_user_upserts_and_me_returns(client):
    user = _login(client)
    r = client.get("/api/me")
    assert r.status_code == 200 and r.json()["email"] == user["email"]


# ---- C: stripe webhook ------------------------------------------------------


def test_webhook_upgrades_then_downgrades():
    user = db.upsert_user("p@x.com", "P", "google", "1")
    stripe_routes.handle_event({
        "type": "checkout.session.completed",
        "data": {"object": {"metadata": {"user_id": str(user["id"]), "plan": "pro"}, "customer": "cus_1"}},
    })
    assert db.get_user(user["id"])["plan"] == "pro"
    stripe_routes.handle_event({
        "type": "customer.subscription.deleted", "data": {"object": {"customer": "cus_1"}},
    })
    assert db.get_user(user["id"])["plan"] == "free"


def test_checkout_requires_auth(client):
    assert client.post("/api/billing/checkout", json={"plan": "pro"}).status_code == 401


# ---- D: dashboard gating ----------------------------------------------------


def test_dashboard_api_gated(client):
    assert client.get("/api/dashboard").status_code == 401
    _login(client)
    r = client.get("/api/dashboard")
    assert r.status_code == 200 and r.json()["user"]["plan"] == "free"
    assert "subscription" in r.json() and "downloads" in r.json()
    assert "caps" not in r.json()  # managed: no user-facing hard caps


def test_dashboard_page_redirects_anon(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code in (302, 307) and "/login" in r.headers["location"]
