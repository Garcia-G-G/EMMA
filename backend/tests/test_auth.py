"""LANDING-27 — email/password auth (register, login, logout, reset, rate-limit).

Run: ``.venv/bin/python -m pytest backend/tests/test_auth.py -q``. No external deps:
password hashing is stdlib pbkdf2, email is regex-validated, reset email logs (no Resend key).
"""

from __future__ import annotations

import hashlib
import os
import tempfile

os.environ.setdefault("DATABASE_URL", tempfile.mktemp(suffix=".db"))

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from backend import auth, auth_local, db, passwords
from backend.app import app
from backend.config import settings
from backend.passwords import hash_password, password_problem, verify_password


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "PUBLIC_URL", "http://localhost")
    db.init_db()
    auth_local._LOGIN_FAILS.clear()  # rate-limit state is process-global
    yield


@pytest.fixture
def client():
    return TestClient(app)


# ---- password primitives ----------------------------------------------------


def test_hash_roundtrips_and_is_salted():
    stored = hash_password("correcthorse9")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("correcthorse9", stored)
    assert not verify_password("wrong", stored)
    # same password hashes differently each time (random salt)
    assert hash_password("correcthorse9") != stored


def test_new_hash_uses_current_work_factor():
    assert hash_password("correcthorse9").split("$")[1] == "600000"


def test_legacy_hash_needs_rehash():
    assert hasattr(passwords, "password_needs_rehash")
    salt = b"0123456789abcdef"
    digest = hashlib.pbkdf2_hmac("sha256", b"correcthorse9", salt, 240_000)
    legacy = f"pbkdf2_sha256$240000${salt.hex()}${digest.hex()}"

    assert verify_password("correcthorse9", legacy)
    assert passwords.password_needs_rehash(legacy)


def test_overlong_password_is_rejected_before_pbkdf2(monkeypatch):
    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("PBKDF2 should not run")

    monkeypatch.setattr(hashlib, "pbkdf2_hmac", fail_if_called)
    password = "ab12" * 300
    assert password_problem(password)
    assert not verify_password(password, "pbkdf2_sha256$240000$00$00")
    assert not called


def test_password_problem_rejects_weak():
    assert password_problem("short")  # < 8 chars
    assert password_problem("aaaaaaaa")  # < 4 unique chars
    assert password_problem(None) is not None
    assert password_problem("correcthorse9") is None


# ---- register ---------------------------------------------------------------


def test_register_sets_cookie_and_creates_local_user(client):
    r = client.post("/api/auth/register", json={"email": "Ana@b.com", "password": "correcthorse9"})
    assert r.status_code == 200
    assert "emma_session" in r.cookies
    row = db.get_user_by_email("ana@b.com")  # stored lowercased
    assert row and row["provider"] == "local" and row["password_hash"]


def test_production_cookie_uses_host_prefix_and_secure_attributes(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://api.example.test")
    response = Response()

    auth.set_session_cookie(response, 42)

    headers = response.headers.getlist("set-cookie")
    header = headers[0]
    assert "__Host-emma_session=" in header
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "Path=/" in header
    assert "samesite=lax" in header.lower()
    assert any(
        item.startswith("emma_session=") and "Max-Age=0" in item
        for item in headers
    )  # clear the legacy cookie during migration
    assert response.headers["cache-control"] == "no-store"


def test_cross_origin_cookie_mutation_is_rejected(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    response = client.post(
        "/api/me/email",
        headers={"Origin": "https://attacker.example"},
        json={"email": "new@b.com"},
    )
    assert response.status_code == 403


def test_same_origin_cookie_mutation_succeeds(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    response = client.post(
        "/api/me/email",
        headers={"Origin": "http://localhost"},
        json={"email": "new@b.com"},
    )
    assert response.status_code == 200


def test_register_persists_name_for_greeting(client):
    # register.html collects a name; it must be stored so the dashboard greets
    # "Hola, <name>" instead of falling back to the email.
    r = client.post(
        "/api/auth/register",
        json={"name": "Gilber", "email": "g@b.com", "password": "correcthorse9"},
    )
    assert r.status_code == 200 and r.json()["name"] == "Gilber"
    assert db.get_user_by_email("g@b.com")["name"] == "Gilber"


def test_register_rejects_weak_password(client):
    r = client.post("/api/auth/register", json={"email": "a@b.com", "password": "abc"})
    assert r.status_code == 400


def test_register_rejects_bad_email(client):
    r = client.post(
        "/api/auth/register", json={"email": "not-an-email", "password": "correcthorse9"}
    )
    assert r.status_code == 422


def test_register_duplicate_is_409(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    r = client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    assert r.status_code == 409


# ---- login / logout ---------------------------------------------------------


def test_login_succeeds_then_whoami(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"email": "a@b.com", "password": "correcthorse9"})
    assert r.status_code == 200 and "emma_session" in r.cookies
    who = client.get("/api/auth/whoami")
    assert who.status_code == 200 and who.json()["user"]["email"] == "a@b.com"


def test_login_upgrades_legacy_hash(client):
    salt = b"0123456789abcdef"
    digest = hashlib.pbkdf2_hmac("sha256", b"correcthorse9", salt, 240_000)
    legacy = f"pbkdf2_sha256$240000${salt.hex()}${digest.hex()}"
    user = db.create_local_user("legacy@example.test", legacy)

    response = client.post(
        "/api/auth/login",
        json={"email": "legacy@example.test", "password": "correcthorse9"},
    )

    assert response.status_code == 200
    upgraded = db.get_user(user["id"])["password_hash"]
    assert upgraded.split("$")[1] == "600000"


def test_login_wrong_password_is_constant_401(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    client.cookies.clear()
    miss = client.post("/api/auth/login", json={"email": "a@b.com", "password": "nope12345"})
    nouser = client.post("/api/auth/login", json={"email": "ghost@b.com", "password": "nope12345"})
    # no account-enumeration: same status + same body for "bad password" and "no such user"
    assert miss.status_code == nouser.status_code == 401
    assert miss.json()["detail"] == nouser.json()["detail"]


def test_login_missing_user_still_runs_password_verification(client, monkeypatch):
    calls: list[str] = []

    def fake_verify(_password: str, stored: str) -> bool:
        calls.append(stored)
        return False

    monkeypatch.setattr(auth_local, "verify_password", fake_verify)
    response = client.post(
        "/api/auth/login",
        json={"email": "missing@example.test", "password": "wrong12345"},
    )

    assert response.status_code == 401
    assert len(calls) == 1
    assert calls[0].startswith("pbkdf2_sha256$")


def test_logout_clears_cookie(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    assert client.get("/api/auth/whoami").json()["authenticated"] is False


def test_get_auth_logout_redirects_not_404(client):
    # The nav + dashboard "Cerrar sesión" link points at GET /auth/logout. It must
    # NOT be shadowed by the dynamic /auth/{provider} route (which would resolve
    # provider="logout" and 404). It should redirect to the public landing and
    # clear the session cookie.
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    r = client.get("/auth/logout", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "theemmafamily.com" in r.headers.get("location", "")
    assert client.get("/api/auth/whoami").json()["authenticated"] is False


def test_login_rate_limited_after_5_fails(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    client.cookies.clear()
    for _ in range(5):
        client.post("/api/auth/login", json={"email": "a@b.com", "password": "wrong12345"})
    sixth = client.post("/api/auth/login", json={"email": "a@b.com", "password": "wrong12345"})
    assert sixth.status_code == 429
    # even the correct password is blocked while throttled
    good = client.post("/api/auth/login", json={"email": "a@b.com", "password": "correcthorse9"})
    assert good.status_code == 429


# ---- password reset ---------------------------------------------------------


def test_reset_request_never_enumerates(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    known = client.post("/api/auth/reset-request", json={"email": "a@b.com"})
    unknown = client.post("/api/auth/reset-request", json={"email": "ghost@b.com"})
    assert known.status_code == unknown.status_code == 200


@pytest.mark.asyncio
async def test_reset_email_closes_http_client(monkeypatch):
    import httpx

    state = {"exited": False, "posted": False}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            state["exited"] = True

        async def post(self, *_args, **_kwargs):
            state["posted"] = True

    monkeypatch.setattr(settings, "RESEND_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    await auth_local._send_reset_email("alex@example.test", "https://example.test/reset")

    assert state == {"exited": True, "posted": True}


def test_reset_confirm_changes_password(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    client.post("/api/auth/reset-request", json={"email": "a@b.com"})
    row = db.get_user_by_email("a@b.com")
    token = row["reset_token"]
    assert token
    r = client.post("/api/auth/reset-confirm", json={"token": token, "new_password": "brandnew321"})
    assert r.status_code == 200
    client.cookies.clear()
    assert (
        client.post(
            "/api/auth/login", json={"email": "a@b.com", "password": "brandnew321"}
        ).status_code
        == 200
    )
    # token is single-use
    again = client.post(
        "/api/auth/reset-confirm", json={"token": token, "new_password": "another4567"}
    )
    assert again.status_code == 400
