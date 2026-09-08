"""LAUNCH-11 Part 2 — the user's OpenAI key lives in Keychain and nowhere else.

This is the product claim of the BYO-key tier, so it is asserted rather than
described. The key must never reach: memory.db, any log, a crash report,
diagnose_self output, the system prompt / priming block, or any request to the
backend — in any form, including hashed or truncated.

The truncated case is the one worth naming. ``core/redaction.py`` already knew
about ``sk-`` via ``_KEY_PREFIXES``, but only reached that check for a 32+ char
run (``_API_KEY_RE``), so the single most likely accidental leak — someone
logging the first 12 characters "just to debug" — sailed straight through.
"""

from __future__ import annotations

import pytest

from core import redaction

_KEY = "sk-" + "K" * 45
_PROJ_KEY = "sk-proj-" + "Q" * 60
_TRUNCATED = "sk-abcdefgh"


# ---- redaction covers sk- at every length ----------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        _KEY,
        _PROJ_KEY,
        _TRUNCATED,
        "sk-" + "b" * 20,
        "sk-svcacct-" + "z" * 30,
    ],
)
def test_every_openai_key_shape_is_redacted(raw: str) -> None:
    out = redaction.redact(raw)
    assert raw not in out, f"key survived redaction: {out}"
    assert "REDACTED" in out


def test_a_truncated_key_in_a_sentence_is_redacted() -> None:
    """'log the first 12 chars for debugging' is how this actually leaks."""
    out = redaction.redact(f"using key {_TRUNCATED}… for the call")
    assert _TRUNCATED not in out
    assert "for the call" in out  # the rest of the message survives


def test_redaction_does_not_eat_ordinary_prose() -> None:
    """The rule is tight enough not to mangle text the model needs."""
    for benign in (
        "sk",
        "the sk- prefix",
        "risk-averse",
        "task-runner",
        "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",  # hex digest, deliberately kept
    ):
        assert redaction.redact(benign) == benign, benign


# ---- the storage tiers -----------------------------------------------------


def _isolated_memory(tmp_path, monkeypatch):
    """A fresh memory.db with embeddings stubbed — the embed call is itself an
    egress path, so a test that hits it would be shipping the key to prove the
    key is not shipped."""
    from memory import embeddings, long_term

    db = tmp_path / "memory.db"
    monkeypatch.setattr(long_term.settings, "MEMORY_DB_PATH", db)
    monkeypatch.setattr(long_term, "_CONN", None, raising=False)

    seen: list[str] = []

    async def _fake_embed(text: str):
        seen.append(text)
        return [0.01] * embeddings.EMBED_DIMS

    monkeypatch.setattr(embeddings, "embed", _fake_embed)
    monkeypatch.setattr(long_term.embeddings, "embed", _fake_embed)
    return db, long_term, seen


def test_key_is_never_written_to_memory_db(tmp_path, monkeypatch) -> None:
    """Personal tier stores facts. A Secret-tier value must never land there."""
    import asyncio

    db, long_term, _ = _isolated_memory(tmp_path, monkeypatch)
    asyncio.run(long_term.remember(f"mi api key es {_KEY}", source="test"))

    raw = db.read_bytes().decode("utf-8", errors="ignore")
    assert _KEY not in raw
    assert "sk-KKKK" not in raw


def test_key_is_never_sent_to_the_embedding_api(tmp_path, monkeypatch) -> None:
    """remember() embeds before it stores, so the embed call is an egress path
    of its own — redaction has to happen BEFORE it, not just before the insert."""
    import asyncio

    _, long_term, embedded = _isolated_memory(tmp_path, monkeypatch)
    asyncio.run(long_term.remember(f"mi api key es {_KEY}", source="test"))

    assert embedded, "nothing was embedded — test fixture drifted"
    for text in embedded:
        assert _KEY not in text


def test_key_never_reaches_the_priming_block(tmp_path, monkeypatch) -> None:
    """The priming block is injected into the system prompt on every session."""
    import asyncio

    _, long_term, _ = _isolated_memory(tmp_path, monkeypatch)
    asyncio.run(long_term.remember(f"la key es {_KEY}", source="test"))

    block = asyncio.run(long_term.priming_block())
    assert _KEY not in block


def test_key_is_scrubbed_from_log_events() -> None:
    """The structlog processor runs on every event before it reaches disk."""
    event = {
        "event": "session_start",
        "detail": f"Authorization: Bearer {_KEY}",
        "short": _TRUNCATED,
    }
    out = redaction.redaction_processor(None, "info", dict(event))
    assert _KEY not in str(out)
    assert _TRUNCATED not in str(out)


def test_key_is_scrubbed_from_a_crash_report() -> None:
    """Crash reports land in ~/Library/Logs/Emma/crashes/ and carry the
    exception message, the traceback, the last transcript AND a log tail —
    four ways for a key to reach a file the user is invited to send someone."""
    from core import crash_handler

    try:
        raise RuntimeError(f"boom while using {_KEY}")
    except RuntimeError as exc:
        text = crash_handler._format_report(
            exc, {"last_transcript": f"mi key es {_TRUNCATED}", "turn_id": "t1"}
        )

    assert _KEY not in text
    assert _TRUNCATED not in text


# ---- entering the key: direct to OpenAI, into Keychain, write-only ----------


def test_validation_calls_openai_directly_never_the_backend(monkeypatch) -> None:
    """Validating a user's personal key through our own proxy is the single
    thing this tier promises never to do."""
    import asyncio

    import httpx

    from core import byok

    seen: list[str] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            seen.append(str(url))
            assert "theemmafamily.com" not in str(url)
            return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    ok, _ = asyncio.run(byok.validate(_KEY))

    assert ok is True
    assert seen == ["https://api.openai.com/v1/models"]


def test_a_rejected_key_is_reported_as_rejected(monkeypatch) -> None:
    import asyncio

    import httpx

    from core import byok

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return httpx.Response(401, json={})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    ok, msg = asyncio.run(byok.validate(_KEY))
    assert ok is False
    assert "rechaz" in msg.lower()


def test_a_network_failure_is_not_reported_as_a_bad_key(monkeypatch) -> None:
    """Telling someone their key is invalid because their wifi dropped sends
    them to regenerate a key that was fine."""
    import asyncio

    import httpx

    from core import byok

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            raise httpx.ConnectError(f"no route to host for {url} with {_KEY}")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    ok, msg = asyncio.run(byok.validate(_KEY))
    assert ok is False
    assert "conexión" in msg.lower()
    assert _KEY not in msg  # the exception text carried it; the message must not


def test_the_key_goes_to_keychain_and_nowhere_else(monkeypatch) -> None:
    import asyncio

    from core import byok

    stored: dict[str, str] = {}

    async def _store(label, value, kind="secret"):
        stored[label] = value

    monkeypatch.setattr("core.secrets.store", _store)
    asyncio.run(byok.store(_KEY))

    assert stored == {"OPENAI_API_KEY": _KEY}


def test_the_masked_form_reveals_only_four_characters() -> None:
    from core import byok

    m = byok.masked(_KEY)
    assert m == "sk-…KKKK"
    assert _KEY not in m
    assert len(m) < 12


def test_there_is_no_readback_control_command() -> None:
    """Write-only means write-only: no control command may return the key.

    A 'just for the settings UI' getter is how a Secret-tier value ends up on an
    IPC channel, in a log, and eventually in a screenshot.
    """
    import inspect

    from dashboard import server

    source = inspect.getsource(server.dispatch_control)
    for forbidden in ("OPENAI_API_KEY", "retrieve(", "retrieve_sync("):
        assert forbidden not in source, f"dispatch_control can read secrets: {forbidden}"


def test_the_module_exposes_no_key_getter() -> None:
    from core import byok

    exported = [n for n in dir(byok) if not n.startswith("_")]
    for name in exported:
        assert "get" not in name.lower() or "hint" in name.lower(), name
    assert not hasattr(byok, "get_key")
    assert not hasattr(byok, "read")


# ---- onboarding forks, and both tiers stay reachable -----------------------


def _page() -> str:
    from pathlib import Path

    return Path("dashboard/index.html").read_text(encoding="utf-8")


def test_onboarding_offers_both_tiers() -> None:
    page = _page()
    for marker in ("ob-choose", "ob-pick-byok", "ob-pick-managed", "ob-key"):
        assert f'id="{marker}"' in page, marker


def test_the_byo_path_says_emma_cannot_report_usage() -> None:
    """A user who expects spend numbers in the app and does not find them files
    it as a bug. Say it in the choice itself, not in a footnote."""
    page = _page()
    assert "no puede decirte cuánto llevas gastado" in page


def test_both_tiers_are_reachable_after_onboarding() -> None:
    """Someone who started managed and hit the cost wall must be able to switch
    without reinstalling — and back again."""
    page = _page()
    for marker in ("btn-use-byok", "btn-use-managed", "btn-drop-key"):
        assert f'id="{marker}"' in page, marker


def test_the_key_field_is_never_an_html_input() -> None:
    """The page triggers a NATIVE panel; it must not collect the key itself."""
    page = _page()
    assert "messageHandlers.emma" in page
    # No input element anywhere near the key pane.
    key_pane = page.split('id="ob-key"')[1].split("</div>")[0]
    assert "<input" not in key_pane
