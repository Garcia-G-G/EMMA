"""Step 40.1: GET /api/me/licenses, the account page's read-only licence list."""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("DATABASE_URL", tempfile.mktemp(suffix=".db"))

import pytest
from fastapi.testclient import TestClient

from backend import auth_local, db
from backend.app import app
from backend.config import settings


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


def _register(client, email):
    r = client.post("/api/auth/register", json={"email": email, "password": "correcthorse9"})
    assert r.status_code == 200, r.text
    return db.get_user_by_email(email)["id"]


def test_lists_own_licences_with_activations(client):
    other = TestClient(app)
    other_id = _register(other, "other@b.com")
    db.create_license(other_id, "monthly", "EMMA-OTHR-OTHR-OTHR", 2_000_000_000.0)

    uid = _register(client, "a@b.com")
    lid = db.create_license(uid, "lifetime", "EMMA-AAAA-BBBB-CCCC", None)
    db.record_activation(lid, "install-1", "Mac de prueba")

    r = client.get("/api/me/licenses")
    assert r.status_code == 200
    lics = r.json()["licenses"]
    assert [lic["key"] for lic in lics] == ["EMMA-AAAA-BBBB-CCCC"]
    lic = lics[0]
    assert lic["plan"] == "lifetime"
    assert lic["status"] == "active"
    assert lic["expires_at"] is None
    assert lic["activations_max"] == 3
    assert len(lic["activations"]) == 1
    act = lic["activations"][0]
    assert act["device_name"] == "Mac de prueba"
    assert act["activated_at"] > 0
    assert "install_id" not in act  # the opaque install id never leaves the backend


def test_no_licences_is_an_empty_list(client):
    _register(client, "a@b.com")
    assert client.get("/api/me/licenses").json() == {"licenses": []}


def test_signed_out_is_401(client):
    assert client.get("/api/me/licenses").status_code == 401
