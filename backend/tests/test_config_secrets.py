"""Backend secrets come from /run/secrets/<NAME> files; env is the dev fallback."""

from __future__ import annotations

from backend import config


def test_secret_file_wins_over_env(tmp_path, monkeypatch):
    (tmp_path / "RESEND_API_KEY").write_text("from-file\n")
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "from-env")
    assert config._secret("RESEND_API_KEY") == "from-file"


def test_env_is_the_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECRETS_DIR", tmp_path)
    monkeypatch.setenv("RESEND_API_KEY", "from-env")
    assert config._secret("RESEND_API_KEY") == "from-env"
    monkeypatch.delenv("RESEND_API_KEY")
    assert config._secret("RESEND_API_KEY", "dflt") == "dflt"
