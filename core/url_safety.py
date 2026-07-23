"""SSRF guard for fetching web pages (Prompt 33 audit fix).

deep_research / summarize_page fetch URLs that come from a web search and follow
redirects. Without a host check, a poisoned result — or a redirect — can make Emma
GET internal addresses on the user's machine (localhost dev servers, 192.168.x.x
routers, 169.254.169.254 cloud-metadata). We resolve the host and reject
private/loopback/link-local/reserved targets BEFORE connecting, and re-validate
every redirect hop (a benign external URL can 30x-bounce inward).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx


def host_is_public(host: str) -> bool:
    """True only if every address `host` resolves to is a public, routable IP."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def url_allowed(url: str) -> bool:
    """http/https scheme + a publicly-routable host."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and host_is_public(p.hostname or "")


async def safe_get_text(
    url: str, *, timeout: float, headers: dict[str, str], max_redirects: int = 4
) -> str:
    """GET `url` following redirects manually, validating EACH hop against SSRF.

    Raises ValueError if any hop targets a non-public host or an unsupported scheme,
    httpx.HTTPError on transport/status failure. Returns the final response text.
    """
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
        current = url
        for _ in range(max_redirects + 1):
            if not url_allowed(current):
                raise ValueError(f"blocked url (ssrf guard): {current}")
            r = await client.get(current)
            if r.is_redirect:
                loc = r.headers.get("location")
                if not loc:
                    r.raise_for_status()
                    return r.text
                current = urljoin(current, loc)
                continue
            r.raise_for_status()
            return r.text
    raise ValueError("too many redirects")
