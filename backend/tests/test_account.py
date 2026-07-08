"""LANDING-27 — downloads + account routes (/api/downloads/*, /api/me/*, /api/account).

Downloads are login-gated (telemetry); account routes let a user change password/email
and soft-delete (GDPR). The dashboard payload (/api/account) carries plan + usage in
minutes for the frontend.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", tempfile.mktemp(suffix=".db"))

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.app import app
from backend.config import settings


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "PUBLIC_URL", "http://localhost")
    db.init_db()
    yield


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


def _register(client, email="a@b.com", pw="correcthorse9"):
    client.post("/api/auth/register", json={"email": email, "password": pw})


# ---- downloads --------------------------------------------------------------


def test_download_latest_route_removed(client):
    # CLIENT-INSTALL-PHASE-3 dropped the login-gated .pkg redirect (curl|sh pivot).
    assert client.get("/api/downloads/latest").status_code == 404


def test_changelog_is_public(client):
    body = client.get("/api/downloads/changelog").json()
    assert body["versions"] and "version" in body["versions"][0]


# ---- account / dashboard ----------------------------------------------------


def test_account_requires_login(client):
    assert client.get("/api/account").status_code == 401


def test_account_reports_plan_and_usage(client):
    _register(client)
    body = client.get("/api/account").json()
    assert body["plan"] == "free" and body["email"] == "a@b.com"
    assert body["usage"]["today_min"] == 0.0
    assert body["usage"]["daily_cap_min"] == 1.0  # free = 60s


def test_change_password_requires_current(client):
    _register(client)
    bad = client.post(
        "/api/me/password", json={"current_password": "wrongwrong", "new_password": "brandnew321"}
    )
    assert bad.status_code == 403
    ok = client.post(
        "/api/me/password",
        json={"current_password": "correcthorse9", "new_password": "brandnew321"},
    )
    assert ok.status_code == 200
    # the new password actually works
    client.post("/api/auth/logout")
    assert (
        client.post(
            "/api/auth/login", json={"email": "a@b.com", "password": "brandnew321"}
        ).status_code
        == 200
    )


def test_change_password_rejects_weak(client):
    _register(client)
    r = client.post(
        "/api/me/password", json={"current_password": "correcthorse9", "new_password": "abc"}
    )
    assert r.status_code == 400


def test_change_email(client):
    _register(client)
    r = client.post("/api/me/email", json={"email": "New@b.com"})
    assert r.status_code == 200 and r.json()["email"] == "new@b.com"
    assert db.get_user_by_email("new@b.com") and db.get_user_by_email("a@b.com") is None


def test_change_email_conflict_is_409(client):
    _register(client, email="a@b.com")
    db.create_local_user("taken@b.com", "x")  # someone else already owns it
    r = client.post("/api/me/email", json={"email": "taken@b.com"})
    assert r.status_code == 409


def test_delete_account_soft_deletes_and_logs_out(client):
    _register(client)
    r = client.delete("/api/me")
    assert r.status_code == 200
    # email is anonymized; the original is no longer resolvable
    assert db.get_user_by_email("a@b.com") is None
    assert client.get("/api/account").status_code == 401
