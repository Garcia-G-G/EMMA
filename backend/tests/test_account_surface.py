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


def test_public_pages_have_logged_out_account_menu(client):
    for path in ("/login", "/register", "/plans", "/download"):
        body = client.get(path).text
        assert 'acct-menu' in body, f"{path} missing Cuenta dropdown"
        assert '<li><a href="/login">Entrar</a></li>' in body, f"{path} missing Entrar item"
        assert '<li><a href="/register">Crear cuenta</a></li>' in body, f"{path} missing Crear cuenta item"


def test_legal_pages(client):
    p = client.get("/privacy")
    assert p.status_code == 200
    assert "Privacidad" in p.text
    assert "OpenAI" in p.text and "Stripe" in p.text  # subprocessors disclosed
    t = client.get("/terms")
    assert t.status_code == 200
    assert "Términos" in t.text
    assert "[PLACEHOLDER" in t.text  # entity/jurisdiction left for legal review


def test_footers_link_legal(client):
    for path in ("/login", "/register", "/plans", "/download"):
        body = client.get(path).text
        assert '/privacy' in body and '/terms' in body, f"{path} footer missing legal links"


def test_dashboard_api_managed_shape(client):
    _register(client)
    d = client.get("/api/dashboard").json()
    assert "caps" not in d                       # managed: no user-facing hard caps
    assert d["usage"]["sessions"] == 0
    assert "minutes" in d["usage"]               # minutes, not raw seconds-only
    assert d["subscription"]["plan"] == "free"
    assert "active" in d["subscription"]
    assert set(d["downloads"]) == {"mac", "win"}
