"""Account-surface build (2026-06-30): /login form, Cuenta dropdown, legal pages,
managed-usage dashboard, admin operator view, managed /api/plans.

Static HTML has no unit tests in this repo, so we assert the *served* page content
via TestClient (a real test) + the backend logic directly. Cookie auth: _register()
auto-logs-in (register sets the session cookie; TestClient persists it).
"""
from __future__ import annotations

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


def test_login_page_has_email_password_form(client):
    body = client.get("/login").text
    assert '/api/auth/login' in body
    assert 'type="password"' in body
    assert 'Continuar con Google' in body  # OAuth still present
