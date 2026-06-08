"""Prompt 26.1-B: the `python -m emma.x_setup` CLI (mocked browser + callback)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from emma import x_setup


def test_missing_client_id_prints_guide(monkeypatch, capsys):
    monkeypatch.setattr(x_setup.settings, "X_CLIENT_ID", "")
    rc = x_setup.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "developer.x.com" in out and "X_CLIENT_ID=" in out


def test_full_flow_stores_three_keychain_entries(monkeypatch):
    monkeypatch.setattr(x_setup.settings, "X_CLIENT_ID", "CID")
    monkeypatch.setattr(x_setup.subprocess, "run", MagicMock())  # don't open a browser
    monkeypatch.setattr(
        x_setup.x_oauth,
        "run_callback_server",
        lambda state, port=8723: {"code": "abc", "state": state},
    )
    monkeypatch.setattr(
        x_setup.x_oauth,
        "exchange_code",
        AsyncMock(return_value={"access_token": "AT", "refresh_token": "RT", "expires_in": 7200}),
    )
    stored: dict[str, str] = {}

    async def _store(label, value, kind="secret"):
        stored[label] = value

    monkeypatch.setattr(x_setup.secrets, "store", _store)

    rc = x_setup.main()
    assert rc == 0
    assert stored["X_ACCESS_TOKEN"] == "AT"
    assert stored["X_REFRESH_TOKEN"] == "RT"
    assert int(stored["X_TOKEN_EXPIRES_AT"]) > 0


def test_callback_timeout_is_handled(monkeypatch, capsys):
    monkeypatch.setattr(x_setup.settings, "X_CLIENT_ID", "CID")
    monkeypatch.setattr(x_setup.subprocess, "run", MagicMock())

    def _timeout(state, port=8723):
        raise TimeoutError("no redirect")

    monkeypatch.setattr(x_setup.x_oauth, "run_callback_server", _timeout)
    rc = x_setup.main()
    assert rc == 1
    assert "no recibí la autorización" in capsys.readouterr().out.lower()
