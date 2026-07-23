"""Password hashing — stdlib only (LANDING-27, no new dep).

bcrypt isn't installed and the prompt forbids new deps, so we use
``hashlib.pbkdf2_hmac`` (PBKDF2-SHA256), which is the stdlib's password-grade KDF.
Hashes are stored as ``pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>`` so the
iteration count travels with the hash and can be raised later without breaking
existing logins. Verification is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import os

_ALGO = "pbkdf2_sha256"
_ITERS = 600_000  # Current OWASP floor for PBKDF2-HMAC-SHA256.
_SALT_BYTES = 16
_MAX_PASSWORD_CHARS = 1024


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERS)
    return f"{_ALGO}${_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a stored ``hash_password`` value."""
    if not isinstance(password, str) or len(password) > _MAX_PASSWORD_CHARS:
        return False
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters_s)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def password_needs_rehash(stored: str) -> bool:
    """True when a valid stored hash uses an obsolete algorithm/work factor."""
    try:
        algo, iters_s, _salt_hex, _hash_hex = stored.split("$")
        return algo != _ALGO or int(iters_s) < _ITERS
    except (ValueError, AttributeError):
        return True


def password_problem(password: str | None) -> str | None:
    """A Spanish reason the password is too weak, or None if acceptable.

    Deliberately lenient (length + not-all-same) — NIST guidance favors length
    over composition rules. The real brute-force defense is the login rate limit.
    """
    if not password or len(password) < 8:
        return "La contraseña debe tener al menos 8 caracteres."
    if len(password) > _MAX_PASSWORD_CHARS:
        return f"La contraseña no puede superar {_MAX_PASSWORD_CHARS} caracteres."
    if len(set(password)) < 4:
        return "Esa contraseña es demasiado simple."
    return None
