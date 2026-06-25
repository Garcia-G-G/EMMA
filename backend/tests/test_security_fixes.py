"""Backend audit fixes — regression tests.

Covers: soft-delete auth bypass, end_session idempotency, windowed monthly cap (no
permanent lockout), the insecure-default-secret startup guard, reset-request rate
limit, the hardened client-IP extractor, and the /realtime session-frame filter.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", tempfile.mktemp(suffix=".db"))

import time

import pytest
from fastapi.testclient import TestClient

from backend import auth_local, config, db
from backend.app import app
from backend.config import PLAN_CAPS, settings
from backend.realtime_proxy import _is_client_session_frame


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "PUBLIC_URL", "http://localhost")
    db.init_db()
    auth_local._LOGIN_FAILS.clear()
    yield


@pytest.fixture
def client():
    return TestClient(app)


# ---- soft-delete must revoke the id-based session ---------------------------


def test_soft_deleted_user_cannot_authenticate(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    assert client.get("/api/account").status_code == 200
    # Delete the row directly so the cookie is untouched — isolates the get_user filter.
    uid = db.get_user_by_email("a@b.com")["id"]
    db.soft_delete_user(uid)
    assert db.get_user(uid) is None  # the id lookup the cookie uses now refuses the row
    assert client.get("/api/account").status_code == 401


# ---- end_session is idempotent (no double-billing) --------------------------


def test_end_session_bills_only_once(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    uid = db.get_user_by_email("a@b.com")["id"]
    sid = db.create_session(uid)
    db.end_session(sid, 100, 0, 0, 0.0)
    db.end_session(sid, 100, 0, 0, 0.0)  # re-entrant close (reconnect / double finally)
    assert db.get_user(uid)["monthly_seconds_used"] == 100  # not 200


# ---- monthly cap is a rolling window, not a permanent counter ---------------


def test_user_seconds_month_is_windowed(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    uid = db.get_user_by_email("a@b.com")["id"]
    sid = db.create_session(uid)
    db.end_session(sid, 500, 0, 0, 0.0)
    # age the session past 30 days → it must fall out of the monthly window
    conn = db.connect()
    conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (time.time() - 2_600_000, sid))
    conn.commit()
    conn.close()
    assert db.user_seconds_month(uid) == 0.0
    # but the lifetime counter still shows the old usage (would have locked them out)
    assert db.get_user(uid)["monthly_seconds_used"] == 500


def test_monthly_demo_gate_reopens_after_window(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_BYPASS_TOKEN", "t", raising=False)
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    uid = db.get_user_by_email("a@b.com")["id"]
    db.set_plan(uid, "pro")
    # burn the whole monthly allowance, then age it out of the 30-day window
    sid = db.create_session(uid)
    db.end_session(sid, PLAN_CAPS["pro"]["monthly_seconds"], 0, 0, 0.0)
    conn = db.connect()
    conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (time.time() - 2_600_000, sid))
    conn.commit()
    conn.close()
    # old code read the never-resetting counter → permanent 402; now it reopens
    r = client.post("/demo/sessions", json={"lang": "es"})
    assert r.status_code == 200


# ---- insecure-default-secret startup guard ----------------------------------


def test_assert_secure_secrets_blocks_prod_defaults(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://api.theemmafamily.com")
    monkeypatch.setattr(settings, "JWT_SECRET", config._INSECURE_DEFAULT)
    with pytest.raises(RuntimeError):
        config.assert_secure_secrets()


def test_assert_secure_secrets_allows_overridden_prod(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://api.theemmafamily.com")
    monkeypatch.setattr(settings, "JWT_SECRET", "a-real-strong-secret")
    monkeypatch.setattr(settings, "SESSION_SECRET", "another-real-strong-secret")
    config.assert_secure_secrets()  # no raise


def test_assert_secure_secrets_allows_http_dev(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_URL", "http://localhost:8000")
    monkeypatch.setattr(settings, "JWT_SECRET", config._INSECURE_DEFAULT)
    config.assert_secure_secrets()  # dev over http is fine


# ---- reset-request rate limit -----------------------------------------------


def test_reset_request_is_rate_limited(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    for _ in range(5):
        assert client.post("/api/auth/reset-request", json={"email": "a@b.com"}).status_code == 200
    assert client.post("/api/auth/reset-request", json={"email": "a@b.com"}).status_code == 429


# ---- hardened client-IP extractor -------------------------------------------


def test_client_ip_ignores_spoofed_leftmost_xff():
    from starlette.requests import Request

    from backend.netutil import client_ip

    def _req(headers):
        scope = {"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers],
                 "client": ("10.0.0.9", 0)}
        return Request(scope)

    # spoofed leftmost entry is ignored; the trusted rightmost hop wins
    assert client_ip(_req([("x-forwarded-for", "1.2.3.4, 9.9.9.9")])) == "9.9.9.9"
    # Fly's edge header beats everything
    assert client_ip(_req([("fly-client-ip", "5.6.7.8"), ("x-forwarded-for", "1.2.3.4")])) == "5.6.7.8"
    # no proxy headers → socket peer
    assert client_ip(_req([])) == "10.0.0.9"


# ---- /realtime client session-frame filter ----------------------------------


def test_realtime_drops_client_session_frames():
    assert _is_client_session_frame('{"type": "session.update", "session": {}}') is True
    assert _is_client_session_frame('{"type": "input_audio_buffer.append"}') is False
    assert _is_client_session_frame("not json") is False
