"""LAUNCH-11 Part 4 — licensing that cannot lock the honest user out.

The three properties that matter, in the order they matter:

1. Activate once, then work offline. Emma starting must never require the
   network.
2. Fail OPEN. A licence server that is down, slow, or returning 500 leaves the
   daemon working. The alternative turns our outage into their churn.
3. Not DRM. The source is public. The cache HMAC is integrity — it catches a
   truncated write and a casual edit — and is not pretending to be more.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from core import license as lic

_KEY = "EMMA-LIFE-ABCD-1234"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("EMMA_HOME", str(tmp_path))

    async def _iid():
        return "install-abc"

    monkeypatch.setattr(lic, "install_id", _iid)
    yield


def _client(handler):
    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return handler(url, json)

    return _C


# ---- the happy path, once ---------------------------------------------------


def test_activation_caches_and_then_works_offline(monkeypatch):
    calls: list[str] = []

    def _ok(url, body):
        calls.append(url)
        return httpx.Response(
            200, json={"valid": True, "plan": "lifetime", "expires_at": None}
        )

    monkeypatch.setattr(httpx, "AsyncClient", _client(_ok))
    ok, msg = asyncio.run(lic.activate(_KEY))

    assert ok is True and "activada" in msg.lower()
    assert calls == ["https://api.theemmafamily.com/api/license/activate"]

    # Now unplug the network entirely. Nothing may reach out again.
    def _boom(url, body):
        raise AssertionError("status() must never touch the network")

    monkeypatch.setattr(httpx, "AsyncClient", _client(_boom))
    st = lic.status()
    assert st["licensed"] is True
    assert st["plan"] == "lifetime"
    assert st["reason"] == "active"


def test_activation_sends_no_usage_and_no_key(monkeypatch):
    """The only contact a BYOK daemon has with us — keep it to what it is for."""
    sent: dict = {}

    def _capture(url, body):
        sent.update(body or {})
        return httpx.Response(200, json={"valid": True, "plan": "annual", "expires_at": None})

    monkeypatch.setattr(httpx, "AsyncClient", _client(_capture))
    asyncio.run(lic.activate(_KEY))

    assert set(sent) == {"license_key", "install_id", "device_name"}
    blob = json.dumps(sent)
    assert "sk-" not in blob
    for forbidden in ("usage", "minutes", "seconds", "model", "tokens"):
        assert forbidden not in blob


# ---- fail open --------------------------------------------------------------


def test_a_network_failure_leaves_the_daemon_licensed(monkeypatch):
    def _down(url, body):
        raise httpx.ConnectError("hetzner is on fire")

    monkeypatch.setattr(httpx, "AsyncClient", _client(_down))
    ok, msg = asyncio.run(lic.activate(_KEY))

    assert ok is True, "a network failure must never read as an invalid licence"
    assert "funciona" in msg.lower()
    assert lic.status()["licensed"] is True


def test_a_server_error_leaves_the_daemon_licensed(monkeypatch):
    monkeypatch.setattr(
        httpx, "AsyncClient", _client(lambda u, b: httpx.Response(500, json={}))
    )
    ok, _ = asyncio.run(lic.activate(_KEY))
    assert ok is True
    assert lic.status()["licensed"] is True


def test_no_cache_at_all_is_licensed(monkeypatch):
    """A fresh install that has never activated must still run. Absence of
    evidence is not evidence of piracy."""
    assert lic.cached() is None
    assert lic.status() == {"licensed": True, "reason": "unknown", "plan": None}


def test_an_expired_cache_still_runs(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _client(
            lambda u, b: httpx.Response(
                200, json={"valid": True, "plan": "annual", "expires_at": time.time() - 10}
            )
        ),
    )
    asyncio.run(lic.activate(_KEY))
    st = lic.status()
    assert st["licensed"] is True
    assert st["reason"] == "expired_pending_recheck"


# ---- the one case that IS a no --------------------------------------------


def test_an_explicit_rejection_is_honoured(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _client(lambda u, b: httpx.Response(404, json={"detail": "No encontramos esa licencia."})),
    )
    ok, msg = asyncio.run(lic.activate(_KEY))
    assert ok is False
    assert "no encontramos" in msg.lower()
    assert lic.status()["licensed"] is False


# ---- integrity, honestly scoped --------------------------------------------


def test_a_hand_edited_cache_is_discarded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _client(lambda u, b: httpx.Response(200, json={"valid": True, "plan": "monthly"})),
    )
    asyncio.run(lic.activate(_KEY))

    path = tmp_path / "license.json"
    blob = json.loads(path.read_text())
    blob["payload"]["plan"] = "lifetime"  # a casual upgrade
    path.write_text(json.dumps(blob))

    assert lic.cached() is None  # signature no longer matches
    # And discarding it fails OPEN, not closed.
    assert lic.status()["licensed"] is True


def test_a_truncated_cache_is_discarded(tmp_path):
    (tmp_path / "license.json").write_text('{"payload": {"valid": tr')
    assert lic.cached() is None
    assert lic.status()["licensed"] is True
