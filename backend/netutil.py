"""Shared network helpers. One hardened client-IP extractor for every rate-limit
surface — previously each of session.py / auth_local.py / demo_session.py carried
its own copy and they DRIFTED (session.py trusted the spoofable leftmost XFF). A
single source of truth removes that whole class of bug.
"""

from __future__ import annotations

from fastapi import Request


def client_ip(request: Request) -> str:
    """The visitor's real IP, trusting ONLY the edge-set header.

    The leftmost X-Forwarded-For entry is CLIENT-controlled — a hostile visitor can
    send `X-Forwarded-For: 1.2.3.4` and rotate it to defeat any per-IP limit. Fly
    sets `Fly-Client-IP` to the true peer; prefer it, then the RIGHTMOST XFF hop
    (appended by the trusted edge), then the socket peer.
    """
    fly = request.headers.get("fly-client-ip")
    if fly:
        return fly.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "0.0.0.0"
