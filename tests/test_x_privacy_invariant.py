"""B53 — privacy invariant sweep for the X surface.

X access/refresh tokens and tweet drafts must NEVER reach memory.db's priming
output, the system prompt, or logs. These are standing-guard tests: the
invariants already hold (the generic API_KEY_LIKE redaction rule catches opaque
tokens, and priming excludes ``vault_ref IS NOT NULL``) — these pin them so a
future change can't silently regress the privacy guarantee.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from core import redaction

# Opaque token shapes (>=32 chars of base64-ish charset → matched by API_KEY_LIKE).
_X_ACCESS = "QzVaWmRWQjFhV1Y0YjNZdGEzSjBaWGt0YVc1MFpYSnVZV3c9OjE2MjUwOjE"
_X_REFRESH = "b3V0aF9yZWZyZXNoX3Rva2VuX2Zvcl9lbW1hX3Rlc3Rfc3VpdGVfMTIzNDU"


# ---- B53.1 / B53.3 — token redaction in logs --------------------------------


def test_x_access_token_redacted_after_bearer() -> None:
    out = redaction.redact(f"Authorization: Bearer {_X_ACCESS}")
    assert _X_ACCESS not in out


def test_x_refresh_token_redacted() -> None:
    assert _X_REFRESH not in redaction.redact(f"refresh_token={_X_REFRESH}")


def test_log_processor_scrubs_nested_x_token() -> None:
    """The structlog processor recurses into nested dicts — a token tucked in a
    request-headers field is scrubbed before the line is persisted."""
    ev = redaction.redaction_processor(
        None,
        "info",
        {"event": "x_post", "req": {"headers": {"Authorization": f"Bearer {_X_ACCESS}"}}},
    )
    assert _X_ACCESS not in str(ev)


def test_public_x_handle_is_not_redacted() -> None:
    """NEGATIVE: an @handle is public — redacting it would be wrong (and noisy)."""
    text = "mi cuenta de X es @gilberga"
    assert redaction.redact(text) == text


# ---- B53.2 / B53.4 — priming + system-prompt exclusion ----------------------


@pytest.fixture
def _seeded_db(tmp_path):
    """A memory.db with one public fact and one Secret-tier (vault_ref) fact.
    The secret has HIGHER confidence, so if exclusion failed it would rank
    first — proving the test catches a real leak, not just ranking."""
    db = tmp_path / "mem.db"
    patcher = patch("memory.long_term.settings")
    ms = patcher.start()
    ms.MEMORY_DB_PATH = db
    ms.MEMORY_PRIMING_TOP_N = 10

    from memory import long_term

    long_term._remember_sync("Garcia prefiere tacos al pastor", "preference", 0.90, "explicit")
    sec = long_term._remember_sync("EMMA_X_SECRET_SENTINEL leak", "fact", 0.99, "reflection")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE facts SET vault_ref=? WHERE id=?", ("X_ACCESS_TOKEN", sec))
    conn.commit()
    conn.close()
    yield
    patcher.stop()


@pytest.mark.asyncio
async def test_secret_tier_fact_excluded_from_priming(_seeded_db) -> None:
    from memory.long_term import priming_block

    block = await priming_block()
    assert "tacos al pastor" in block  # the public fact surfaces
    assert "SENTINEL" not in block  # the vault_ref fact is excluded


@pytest.mark.asyncio
async def test_secret_tier_fact_absent_from_system_prompt(_seeded_db) -> None:
    import core.conversation as conv
    from core import session_memory

    session_memory.clear()  # no recent context → flat priming path (no embeddings)
    prompt = await conv._build_instructions()
    assert "SENTINEL" not in prompt
