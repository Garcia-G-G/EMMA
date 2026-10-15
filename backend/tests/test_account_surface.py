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


def test_dashboard_api_managed_shape(client):
    _register(client)
    d = client.get("/api/dashboard").json()
    assert "caps" not in d                       # managed: no user-facing hard caps
    assert d["usage"]["sessions"] == 0
    assert "minutes" in d["usage"]               # minutes, not raw seconds-only
    assert d["subscription"]["plan"] == "free"
    assert "active" in d["subscription"]
    assert set(d["downloads"]) == {"mac", "win"}


def test_admin_overview_requires_login(client):
    assert client.get("/api/admin/overview").status_code in (401, 403)


def test_require_admin_gate(client, monkeypatch):
    _register(client, email="boss@theemmafamily.com", pw="correcthorse9")
    monkeypatch.setattr(settings, "ADMIN_EMAILS", "")
    assert client.get("/api/admin/overview").status_code == 403
    monkeypatch.setattr(settings, "ADMIN_EMAILS", "boss@theemmafamily.com")
    r = client.get("/api/admin/overview")
    assert r.status_code == 200
    assert "spend_today_usd" in r.json()


def test_api_plans_is_managed(client):
    d = client.get("/api/plans").json()
    blob = str(d).lower()
    assert "trae tu api key" not in blob and "byok" not in blob
    assert d["plans"][0]["id"] == "pro"


def test_admin_page_is_admin_gated(client, monkeypatch):
    # anon → redirect to login
    r = client.get("/admin")
    assert r.status_code in (302, 307) and "/entrar" in r.headers.get("location", "")
    # logged-in non-admin → still redirected (page not leaked)
    _register(client, email="nobody@x.com", pw="correcthorse9")
    monkeypatch.setattr(settings, "ADMIN_EMAILS", "")
    r2 = client.get("/admin")
    assert r2.status_code in (302, 307), "non-admin must not get the admin page"
    # admin → 200 page
    monkeypatch.setattr(settings, "ADMIN_EMAILS", "nobody@x.com")
    assert client.get("/admin").status_code == 200


# ---- Step 40.1: one host, ES + EN pages (backend/pages.py) ----


def test_login_page_has_email_password_form(client):
    for path in ("/entrar", "/en/login"):
        body = client.get(path).text
        assert "/api/auth/login" in body and 'type="password"' in body
        assert 'hreflang="en"' in body and 'hreflang="es"' in body


@pytest.mark.parametrize(
    "old,new",
    [("/login", "/entrar"), ("/register", "/registro"), ("/plans", "/precios"),
     ("/download", "/instalar"), ("/privacy", "/privacidad"), ("/terms", "/terminos"),
     ("/auth/reset?token=abc", "/restablecer?token=abc")],
)
def test_old_routes_301(client, old, new):
    r = client.get(old)
    assert r.status_code == 301 and r.headers["location"] == new


def test_account_page_has_settings_and_no_usage(client):
    _register(client)
    body = client.get("/cuenta").text
    for api in ("/api/me/password", "/api/me/email", "/api/me/licenses", "/api/auth/logout"):
        assert api in body
    assert "Minutos" not in body and "minutes" not in body.lower()


def test_pages_make_no_retired_claims(client):
    for path in ("/precios", "/en/pricing", "/instalar", "/en/install", "/privacidad", "/en/privacy",
                 "/terminos", "/en/terms"):
        body = client.get(path).text
        assert client.get(path).status_code == 200, path
        for gone in ("OpenAI", "Realtime", "coral", "$19", "90 segundos", "minutos incluidos"):
            assert gone not in body, f"{path}: {gone}"


def test_pricing_matches_the_licence_plans(client):
    from backend.license_routes import PLANS

    body = client.get("/precios").text
    for plan in PLANS.values():
        assert f"${plan['usd']:g}" in body


def test_install_page_has_the_installer_line(client):
    for path in ("/instalar", "/en/install"):
        body = client.get(path).text
        assert "curl -fsSL https://theemmafamily.com/install.sh | sh" in body
        assert "uninstall.sh" in body

