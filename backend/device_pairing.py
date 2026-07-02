"""PAIR-DEVICE-1 — OAuth 2.0 Device Authorization Grant (RFC 8628).

The Mac daemon pairs with a web account via a short human code. This module is
the backend half; it talks to SQLite through backend.db (raw sqlite, epoch-float
timestamps — matching the rest of the store). Security invariants:

- device_code and access_token are NEVER stored in plaintext — only sha256 hex.
  A DB leak yields useless hashes; the real values live on the daemon (Keychain)
  and are only ever known to the issuing flow.
- The DeviceCode row is one-shot: deleted the instant it's exchanged for a token
  (no replay window).
- user_code is short-lived (10 min) and avoids confusable chars (no I/1/L, O/0).
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import Any

from backend import db

# user_code is human-typed; avoid easily-confused chars (I/1/L, O/0).
_USER_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
_USER_CODE_RE = re.compile(r"^[A-Z2-9]{4}-?[A-Z2-9]{4}$")
_DEVICE_CODE_BYTES = 32   # → 64 hex chars
_DEVICE_TOKEN_BYTES = 32  # → ~43 url-safe chars
_CODE_TTL_SECONDS = 600   # 10 minutes
_MIN_POLL_INTERVAL_SECONDS = 5
_VERIFICATION_URI = "https://theemmafamily.com/pair"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_user_code() -> str:
    def block() -> str:
        return "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(4))
    return f"{block()}-{block()}"


def _normalize(user_code: str) -> str:
    n = user_code.strip().upper()
    if "-" not in n and len(n) == 8:
        n = n[:4] + "-" + n[4:]
    return n


def issue_device_code() -> dict[str, Any]:
    """Step 1 — daemon calls; returns the codes the user types on the web."""
    conn = db.connect()
    try:
        user_code = None
        for _ in range(8):  # collision is ~1/10^11; cheap to retry
            candidate = _new_user_code()
            if conn.execute("SELECT 1 FROM device_codes WHERE user_code=?",
                            (candidate,)).fetchone() is None:
                user_code = candidate
                break
        if user_code is None:
            raise RuntimeError("could not allocate unique user_code")

        device_code = secrets.token_hex(_DEVICE_CODE_BYTES)
        now = time.time()
        conn.execute(
            "INSERT INTO device_codes(user_code,device_code_hash,authorized,expires_at,created_at) "
            "VALUES(?,?,0,?,?)",
            (user_code, _hash(device_code), now + _CODE_TTL_SECONDS, now),
        )
        conn.commit()
        return {
            "user_code": user_code,
            "device_code": device_code,            # ONE-TIME — sent only to the daemon
            "verification_uri": _VERIFICATION_URI,
            "interval": _MIN_POLL_INTERVAL_SECONDS,
            "expires_in": _CODE_TTL_SECONDS,
        }
    finally:
        conn.close()


def authorize_user_code(user_code: str, user_id: int, device_name: str) -> None:
    """Step 4 — the web (authenticated) marks a user_code as approved."""
    if not _USER_CODE_RE.match(user_code.strip().upper()):
        raise ValueError("malformed user_code")
    normalized = _normalize(user_code)
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM device_codes WHERE user_code=?", (normalized,)).fetchone()
        if not row:
            raise LookupError("user_code not found")
        if row["expires_at"] < time.time():
            raise LookupError("expired")
        if row["authorized"]:
            raise LookupError("already used")
        conn.execute(
            "UPDATE device_codes SET authorized=1, user_id=?, device_name=? WHERE id=?",
            (user_id, (device_name or "Emma device")[:120], row["id"]),
        )
        conn.commit()
    finally:
        conn.close()


def exchange_device_code(device_code: str, request_ip: str | None) -> dict[str, Any]:
    """Step 3 — daemon polls; returns 'pending'/'slow_down' or a real access_token."""
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM device_codes WHERE device_code_hash=?",
                           (_hash(device_code),)).fetchone()
        if not row:
            return {"_status": 401, "error": "access_denied"}
        now = time.time()
        if row["expires_at"] < now:
            return {"_status": 401, "error": "expired_token"}

        # Slow-down guard (RFC 8628 §3.5): daemon polling faster than interval.
        if row["last_polled_at"] and (now - row["last_polled_at"]) < (_MIN_POLL_INTERVAL_SECONDS - 1):
            conn.execute("UPDATE device_codes SET last_polled_at=? WHERE id=?", (now, row["id"]))
            conn.commit()
            return {"_status": 429, "error": "slow_down"}
        conn.execute("UPDATE device_codes SET last_polled_at=? WHERE id=?", (now, row["id"]))
        conn.commit()

        if not row["authorized"]:
            return {"_status": 401, "error": "authorization_pending"}

        # Authorized — mint a token and delete the device_code row (one-shot).
        access_token = secrets.token_urlsafe(_DEVICE_TOKEN_BYTES)
        user = db.get_user(row["user_id"])
        conn.execute(
            "INSERT INTO device_tokens(user_id,token_hash,device_name,last_seen_at,last_ip,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (row["user_id"], _hash(access_token), row["device_name"] or "Emma device",
             now, request_ip, now),
        )
        token_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("DELETE FROM device_codes WHERE id=?", (row["id"],))
        conn.commit()
        return {
            "_status": 200,
            "access_token": access_token,
            "device_id": token_id,
            "user": {
                "email": user["email"] if user else None,
                "name": user.get("name") if user else None,
                "plan": (user.get("plan", "free") if user else "free"),
            },
        }
    finally:
        conn.close()


def resolve_token(raw_token: str) -> dict[str, Any] | None:
    """Bearer-auth helper — given the token value, return the device row or None."""
    if not raw_token or len(raw_token) < 20:
        return None
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM device_tokens WHERE token_hash=? AND revoked_at IS NULL",
            (_hash(raw_token),),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE device_tokens SET last_seen_at=? WHERE id=?", (time.time(), row["id"]))
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def revoke_token(token_id: int, user_id: int) -> bool:
    """Web (authenticated) revokes one device."""
    conn = db.connect()
    try:
        cur = conn.execute(
            "UPDATE device_tokens SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
            (time.time(), token_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _iso(ts: float | None) -> str | None:
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).isoformat() if ts else None


def list_devices(user_id: int) -> list[dict[str, Any]]:
    """Web — the dashboard's paired-device list (API shape the dashboard JS reads)."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, device_name, last_seen_at, created_at FROM device_tokens "
            "WHERE user_id=? AND revoked_at IS NULL ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [{"id": r["id"], "name": r["device_name"],
                 "last_seen": _iso(r["last_seen_at"]), "created_at": _iso(r["created_at"])}
                for r in rows]
    finally:
        conn.close()
