"""PAIR-DEVICE-1 — device-pairing core (RFC 8628). Ported to the raw-sqlite store."""
from __future__ import annotations

import re
import time

import pytest

from backend import db
from backend import device_pairing as dp
from backend.config import settings

_USER_CODE_RE = re.compile(r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "t.db"))
    db.init_db()
    yield


def _user(email="a@b.com"):
    return db.create_local_user(email, "x" * 60)


def _expire(device_code_hash):
    conn = db.connect()
    try:
        conn.execute("UPDATE device_codes SET expires_at=? WHERE device_code_hash=?",
                     (time.time() - 1, device_code_hash))
        conn.commit()
    finally:
        conn.close()


def test_issue_shape():
    info = dp.issue_device_code()
    assert _USER_CODE_RE.match(info["user_code"])
    assert re.match(r"^[0-9a-f]{64}$", info["device_code"])
    assert info["expires_in"] == 600
    assert info["interval"] == 5
    assert info["verification_uri"].endswith("/pair")


def test_exchange_unknown():
    assert dp.exchange_device_code("deadbeef" * 8, None)["error"] == "access_denied"


def test_exchange_pending():
    info = dp.issue_device_code()
    r = dp.exchange_device_code(info["device_code"], None)
    assert r["_status"] == 401 and r["error"] == "authorization_pending"


def test_slow_down_on_rapid_poll():
    info = dp.issue_device_code()
    dp.exchange_device_code(info["device_code"], None)          # first poll → pending
    r2 = dp.exchange_device_code(info["device_code"], None)     # immediate second → slow_down
    assert r2["_status"] == 429 and r2["error"] == "slow_down"


def test_authorize_then_exchange_mints_token():
    u = _user()
    info = dp.issue_device_code()
    dp.authorize_user_code(info["user_code"], u["id"], "Test Mac")
    r = dp.exchange_device_code(info["device_code"], "1.2.3.4")
    assert r["_status"] == 200
    assert r["user"]["email"] == "a@b.com"
    # token hash matches a DeviceToken row for the right user
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM device_tokens WHERE token_hash=?",
                           (dp._hash(r["access_token"]),)).fetchone()
    finally:
        conn.close()
    assert row is not None and row["user_id"] == u["id"] and row["device_name"] == "Test Mac"


def test_exchange_is_one_shot():
    u = _user()
    info = dp.issue_device_code()
    dp.authorize_user_code(info["user_code"], u["id"], "M")
    assert dp.exchange_device_code(info["device_code"], None)["_status"] == 200
    # code row deleted → a second exchange is access_denied
    assert dp.exchange_device_code(info["device_code"], None)["error"] == "access_denied"


def test_expired_device_code():
    info = dp.issue_device_code()
    _expire(dp._hash(info["device_code"]))
    assert dp.exchange_device_code(info["device_code"], None)["error"] == "expired_token"


def test_malformed_user_code_raises():
    u = _user()
    with pytest.raises(ValueError):
        dp.authorize_user_code("nope!", u["id"], "M")


def test_resolve_token_none_paths():
    assert dp.resolve_token("") is None
    assert dp.resolve_token("short") is None
    assert dp.resolve_token("z" * 40) is None  # unknown


def test_revoke_then_resolve_none():
    u = _user()
    info = dp.issue_device_code()
    dp.authorize_user_code(info["user_code"], u["id"], "M")
    tok = dp.exchange_device_code(info["device_code"], None)["access_token"]
    assert dp.resolve_token(tok) is not None
    dev = dp.list_devices(u["id"])
    assert len(dev) == 1
    assert dp.revoke_token(dev[0]["id"], u["id"]) is True
    assert dp.resolve_token(tok) is None
    assert dp.revoke_token(dev[0]["id"], u["id"]) is False  # already revoked


# ---- route-level round trip (FastAPI TestClient) ----
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import app  # noqa: E402


def _client():
    return TestClient(app, follow_redirects=False)


def test_route_round_trip():
    c = _client()
    c.post("/api/auth/register", json={"email": "d@e.com", "password": "correcthorse9"})  # logs in
    # 1. daemon asks for a code (no auth)
    info = c.post("/api/device/code").json()
    assert len(info["user_code"]) == 9 and info["expires_in"] == 600
    # 2. web (authed cookie) authorizes it
    a = c.post("/api/device/authorize", json={"user_code": info["user_code"], "device_name": "Mac Test"})
    assert a.status_code == 200
    # 3. daemon polls once → 200 + token (first poll, no slow_down)
    t = c.post("/api/device/token", json={"device_code": info["device_code"]})
    assert t.status_code == 200
    body = t.json()
    assert body["access_token"] and body["user"]["email"] == "d@e.com"
    # 4. one-shot: second exchange fails
    assert c.post("/api/device/token", json={"device_code": info["device_code"]}).status_code == 401
    # 5. device appears in the authed list
    devs = c.get("/api/devices").json()
    assert len(devs) == 1 and devs[0]["name"] == "Mac Test"
    # 6. revoke it
    assert c.delete(f"/api/devices/{devs[0]['id']}").status_code == 200
    assert c.get("/api/devices").json() == []


def test_pending_and_pair_page_gate():
    c = _client()
    info = c.post("/api/device/code").json()
    r = c.post("/api/device/token", json={"device_code": info["device_code"]})
    assert r.status_code == 401 and r.json()["detail"]["error"] == "authorization_pending"
    # /pair redirects to login when not authed
    assert c.get("/pair").status_code in (302, 307)
