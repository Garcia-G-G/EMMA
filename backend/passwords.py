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
_ITERS = 240_000  # OWASP-recommended floor for PBKDF2-SHA256 (2023+)
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERS)
    return f"{_ALGO}${_ITERS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a stored ``hash_password`` value."""
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


def password_problem(password: str) -> str | None:
    """A Spanish reason the password is too weak, or None if acceptable.

    Deliberately lenient (length + not-all-same) — NIST guidance favors length
    over composition rules. The real brute-force defense is the login rate limit.
    """
    if not password or len(password) < 8:
        return "La contraseña debe tener al menos 8 caracteres."
    if len(set(password)) < 4:
        return "Esa contraseña es demasiado simple."
    return None
