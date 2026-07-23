"""LANDING-27 — email/password auth (register, login, logout, reset, rate-limit).

Run: ``.venv/bin/python -m pytest backend/tests/test_auth.py -q``. No external deps:
password hashing is stdlib pbkdf2, email is regex-validated, reset email logs (no Resend key).
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", tempfile.mktemp(suffix=".db"))

import pytest
from fastapi.testclient import TestClient

from backend import auth_local, db
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


def test_login_wrong_password_is_constant_401(client):
    client.post("/api/auth/register", json={"email": "a@b.com", "password": "correcthorse9"})
    client.cookies.clear()
    miss = client.post("/api/auth/login", json={"email": "a@b.com", "password": "nope12345"})
    nouser = client.post("/api/auth/login", json={"email": "ghost@b.com", "password": "nope12345"})
    # no account-enumeration: same status + same body for "bad password" and "no such user"
    assert miss.status_code == nouser.status_code == 401
    assert miss.json()["detail"] == nouser.json()["detail"]


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
