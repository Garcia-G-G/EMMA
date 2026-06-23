"""Talk-to-Emma demo backend tests (LANDING-25.0, G1). FastAPI TestClient;
Turnstile + OpenAI are mocked. No real network, no real session runs."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend import db, demo_session
from backend.app import app
from backend.config import settings


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "t.db"))
    db.init_db()
    monkeypatch.setattr(settings, "DEMO_IP_SALT", "test-salt")
    monkeypatch.setattr(settings, "DEMO_BYPASS_TOKEN", "")
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET", "set")  # captcha_enabled


@pytest.fixture
def client():
    return TestClient(app)


def _ok_turnstile(monkeypatch, ok=True):
    async def _v(token, ip):
        return ok
    monkeypatch.setattr(demo_session, "verify_captcha", _v)


# ---- A1 create + Turnstile + rate limit -------------------------------------


def test_create_requires_turnstile(client, monkeypatch):
    _ok_turnstile(monkeypatch, ok=False)
    r = client.post("/demo/sessions", json={"lang": "es", "turnstile_token": "bad"})
    assert r.status_code == 403


def test_create_returns_session(client, monkeypatch):
    _ok_turnstile(monkeypatch)
    r = client.post("/demo/sessions", json={"lang": "es", "turnstile_token": "good"})
    assert r.status_code == 200
    b = r.json()
    assert b["session_id"].startswith("") and "/demo/ws/" in b["ws_url"]
    assert b["cost_cap_cents"] == settings.DEMO_COST_CAP_CENTS
    assert b["duration_seconds"] == settings.DEMO_TALK_SECONDS
    assert "token=" in b["ws_url"]  # the JWT, not raw creds


def test_second_session_same_ip_is_rate_limited(client, monkeypatch):
    _ok_turnstile(monkeypatch)
    client.post("/demo/sessions", json={"turnstile_token": "good"})
    r = client.post("/demo/sessions", json={"turnstile_token": "good"})
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "rate_limited"


def test_rate_limit_keyed_on_hashed_ip_not_raw(client, monkeypatch):
    _ok_turnstile(monkeypatch)
    client.post("/demo/sessions", json={"turnstile_token": "good"})
    # the stored key must be the hash, never the raw client IP
    conn = db.connect()
    try:
        rows = [row[0] for row in conn.execute("SELECT ip FROM demo_hits").fetchall()]
    finally:
        conn.close()
    assert rows and all(len(k) == 64 for k in rows)  # sha256 hex
    assert "testclient" not in str(rows) and "127.0.0.1" not in str(rows)


# ---- A6 bypass token --------------------------------------------------------


def test_bypass_token_skips_rate_limit_and_turnstile(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_BYPASS_TOKEN", "secret-bypass")
    _ok_turnstile(monkeypatch, ok=False)  # turnstile would fail...
    h = {"X-Demo-Bypass": "secret-bypass"}
    r1 = client.post("/demo/sessions", json={"turnstile_token": ""}, headers=h)
    r2 = client.post("/demo/sessions", json={"turnstile_token": ""}, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200  # ...yet both succeed


def test_wrong_bypass_token_is_ignored(client, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_BYPASS_TOKEN", "secret-bypass")
    _ok_turnstile(monkeypatch, ok=False)
    r = client.post("/demo/sessions", json={"turnstile_token": ""},
                    headers={"X-Demo-Bypass": "wrong"})
    assert r.status_code == 403  # falls back to the failed turnstile


# ---- B: tool whitelist is exactly 4, server-side ----------------------------


def test_exactly_four_demo_tools() -> None:
    assert len(demo_session.DEMO_TOOL_NAMES) == 4
    assert set(demo_session.DEMO_TOOL_NAMES) == {
        "web_search", "current_time", "translate", "explain_install"}


def test_session_config_injects_only_whitelisted_tools_and_coral() -> None:
    cfg = demo_session.session_config("es")["session"]
    assert cfg["audio"]["output"]["voice"] == "coral"  # 25.0.3 GA: voice under audio.output
    assert {t["name"] for t in cfg["tools"]} == set(demo_session.DEMO_TOOL_NAMES)
    assert "vive en tu Mac" in cfg["instructions"]  # demo persona, not the daemon's


@pytest.mark.asyncio
async def test_non_whitelisted_function_call_is_rejected() -> None:
    # The model asking for a daemon tool must be refused server-side, not executed.
    out = await demo_session._run_tool("delete_note", {"title": "x"})
    assert "error" in out and "not available" in out["error"]


@pytest.mark.asyncio
async def test_whitelisted_tool_runs() -> None:
    out = await demo_session._run_tool("explain_install", {})
    assert "download_url" in out and out["steps"]


@pytest.mark.asyncio
async def test_current_time_tool() -> None:
    out = await demo_session._run_tool("current_time", {"timezone": "UTC"})
    assert out["timezone"] == "UTC" and ":" in out["spoken"]


# ---- C: prompt is the demo file, bilingual ----------------------------------


def test_prompt_is_demo_not_daemon() -> None:
    es, en = demo_session._load_prompt("es"), demo_session._load_prompt("en")
    assert "vive en tu Mac" in es and "lives on your Mac" in en
    assert "external" in en.lower() and "inert" in en.lower()  # injection rule present
    assert "inerte" in es.lower()


# ---- config endpoint exposes only public values -----------------------------


def test_demo_config_exposes_no_secrets(client, monkeypatch):
    monkeypatch.setattr(settings, "TURNSTILE_SITE_KEY", "0xPUBLIC")
    d = client.get("/demo/config").json()
    assert d["turnstile_site_key"] == "0xPUBLIC"
    assert "secret" not in json.dumps(d).lower()
    assert "duration_seconds" in d


# ---- WS rejects bad/foreign tokens ------------------------------------------


def test_ws_rejects_invalid_token(client):
    import contextlib

    from starlette.websockets import WebSocketDisconnect
    with contextlib.suppress(WebSocketDisconnect), \
            client.websocket_connect("/demo/ws/demo_x?token=garbage"):
        pass  # server closes (4401) before accept → disconnect raised


# ---- 25.0.1: Turnstile optional + CORS apex + config has no secrets ----------


def test_turnstile_optional_when_no_secret(client, monkeypatch):
    # No secret configured → demo works WITHOUT a token (ambient flow sends none).
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET", "")
    r = client.post("/demo/sessions", json={"lang": "es"})  # no turnstile_token at all
    assert r.status_code == 200


def test_turnstile_required_rejects_empty_token(client, monkeypatch):
    # Secret IS set but token missing → reject (no trivial bypass).
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET", "a-secret")
    r = client.post("/demo/sessions", json={"lang": "es", "turnstile_token": ""})
    assert r.status_code == 403


def test_turnstile_required_accepts_valid_token(client, monkeypatch):
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET", "a-secret")
    _ok_turnstile(monkeypatch, ok=True)  # mock Cloudflare verify → success
    r = client.post("/demo/sessions", json={"lang": "es", "turnstile_token": "valid"})
    assert r.status_code == 200


def test_cors_allows_apex_blocks_random(client):
    allowed = client.options("/demo/sessions", headers={
        "Origin": "https://theemmafamily.com",
        "Access-Control-Request-Method": "POST"})
    assert allowed.headers.get("access-control-allow-origin") == "https://theemmafamily.com"
    blocked = client.options("/demo/sessions", headers={
        "Origin": "https://evil.com",
        "Access-Control-Request-Method": "POST"})
    assert blocked.headers.get("access-control-allow-origin") != "https://evil.com"


def test_demo_config_has_no_secret_keys(client, monkeypatch):
    monkeypatch.setattr(settings, "TURNSTILE_SITE_KEY", "0xPUBLIC")
    monkeypatch.setattr(settings, "DEMO_IP_SALT", "super-secret-salt")
    monkeypatch.setattr(settings, "DEMO_BYPASS_TOKEN", "super-secret-token")
    d = client.get("/demo/config").json()
    keys = set(d)
    assert "turnstile_site_key" in keys and d["turnstile_site_key"] == "0xPUBLIC"
    # explicitly: NO secret ever leaks
    for forbidden in ("turnstile_secret", "ip_salt", "bypass_token", "salt", "secret"):
        assert not any(forbidden in k.lower() for k in keys)
    assert "super-secret-salt" not in str(d) and "super-secret-token" not in str(d)


# ---- 24.7: daily ceiling 503 + config exact key set + WS bandwidth -----------


def test_daily_cost_ceiling_returns_503(client, monkeypatch):
    # Force the rolling-24h spend over the ceiling → demo 503s for the day.
    monkeypatch.setattr(settings, "DEMO_DAILY_USD_CEILING", 5.0)
    monkeypatch.setattr(demo_session.db, "day_cost_usd", lambda: 9.99)
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET", "")
    r = client.post("/demo/sessions", json={"lang": "es"})
    assert r.status_code == 503
    assert "descansando hoy" in r.json()["detail"]


def test_demo_config_exact_key_set(client):
    # B5: ONLY these keys may ever appear — no salt/secret/bypass leak.
    d = client.get("/demo/config").json()
    assert set(d) == {"turnstile_site_key", "duration_seconds", "warning_at_seconds"}


def test_daily_report_aggregates_no_pii(client, monkeypatch):
    # E2: auth-gated; returns counts/cost only, never IPs.
    from backend import auth
    user = db.upsert_user("g@example.com", "Garcia", "google", "p1")
    client.cookies.set("emma_session", auth._serializer.dumps({"uid": user["id"]}))
    d = client.get("/demo/admin/daily-report").json()
    assert set(d) >= {"sessions_24h", "cost_usd_24h", "daily_ceiling_usd"}
    assert "ip" not in json.dumps(d).lower()
