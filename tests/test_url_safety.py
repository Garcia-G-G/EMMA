"""Prompt 33 audit — SSRF guard for page fetching (literal IPs, no DNS needed)."""

from __future__ import annotations

from core import url_safety as us


def test_rejects_loopback_and_private_and_metadata() -> None:
    assert us.url_allowed("http://127.0.0.1/") is False
    assert us.url_allowed("http://10.0.0.1/admin") is False
    assert us.url_allowed("http://192.168.1.1/") is False
    assert us.url_allowed("http://169.254.169.254/latest/meta-data/") is False  # cloud metadata
    assert us.url_allowed("http://[::1]/") is False


def test_rejects_non_http_schemes() -> None:
    assert us.url_allowed("file:///etc/passwd") is False
    assert us.url_allowed("ftp://8.8.8.8/x") is False
    assert us.url_allowed("gopher://8.8.8.8/") is False


def test_allows_public_ip() -> None:
    assert us.url_allowed("https://8.8.8.8/") is True
    assert us.url_allowed("http://1.1.1.1/x") is True


def test_host_is_public_literal_ips() -> None:
    assert us.host_is_public("127.0.0.1") is False
    assert us.host_is_public("8.8.8.8") is True
    assert us.host_is_public("") is False
