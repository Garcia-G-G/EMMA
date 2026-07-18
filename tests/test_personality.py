"""EMMA-APP Part 4 — personality axes → # Personality section.

The load-bearing guarantee: at default axes the prompt is byte-for-byte today's,
so a user who never opens the panel gets zero behavior change. Personality is
taste — it must not touch Language / Confirmation / Tool Results (correctness).
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from core import dictionary, personality


def test_defaults_are_byte_for_byte_todays_bullets() -> None:
    assert personality.personality_lines() == [
        "- Confident, calm, slightly witty. Never flustered.",
        "- Talk to Garcia like a trusted colleague, not a customer.",
        "- Be direct. No filler, no hedging, no apologies.",
    ]
    # explicit all-default profile is identical
    assert personality.personality_lines(dict.fromkeys(personality.AXES, 3)) == list(
        personality.BASE_LINES
    )


def test_off_default_axis_appends_one_line_each() -> None:
    lines = personality.personality_lines({"calidez": 5, "humor": 1})
    assert lines[:3] == list(personality.BASE_LINES)  # base untouched
    assert any("warm" in low.lower() for low in lines[3:])
    assert any("humor" in low.lower() for low in lines[3:])
    assert len(lines) == 5  # exactly two appended


def test_normalize_clamps_and_fills() -> None:
    n = personality.normalize({"humor": 9, "calidez": 0, "bogus": 3})
    assert n["humor"] == 5 and n["calidez"] == 1
    assert n["formalidad"] == 3  # missing -> default
    assert "bogus" not in n


def test_full_prompt_unchanged_at_defaults() -> None:
    # The whole # Personality section, as built, equals the three default bullets
    # (name-substituted) with nothing appended.
    with patch.object(dictionary, "user_profile", lambda: {"preferred_lang": "es"}), patch.object(
        dictionary, "personality_profile", lambda: dict.fromkeys(personality.AXES, 3)
    ):
        loop = asyncio.new_event_loop()
        try:
            import core.conversation as conv

            instr = loop.run_until_complete(conv._build_instructions())
        finally:
            loop.close()
    assert "- Confident, calm, slightly witty. Never flustered." in instr
    assert "- Be direct. No filler, no hedging, no apologies." in instr
    # no off-default guidance leaked in
    assert "response-length range" not in instr
    assert "Prefer the shortest" not in instr


def test_personality_never_touches_correctness_sections() -> None:
    # Even at an extreme profile, the Language + Confirmation sections are intact.
    with patch.object(dictionary, "user_profile", lambda: {"preferred_lang": "es"}), patch.object(
        dictionary, "personality_profile", lambda: {"calidez": 5, "formalidad": 5, "humor": 5,
                                                     "verbosidad": 5, "proactividad": 5}
    ):
        loop = asyncio.new_event_loop()
        try:
            import core.conversation as conv

            instr = loop.run_until_complete(conv._build_instructions())
        finally:
            loop.close()
    assert "# Language" in instr
    assert "# Confirmation flow" in instr  # correctness section present, unaltered
    assert "response-length range" in instr  # verbosidad nudges WITHIN limits (stated)


def test_storage_round_trip(tmp_path, monkeypatch) -> None:
    toml = tmp_path / "dictionary.toml"
    toml.write_text('[user]\ndisplay_name = ""\n\n[pages.x]\nurl = "http://x"\n', encoding="utf-8")
    monkeypatch.setattr(dictionary, "_DICT_PATH", toml)
    dictionary.reload()
    assert dictionary.personality_profile()["humor"] == 3  # default when absent
    assert dictionary.set_personality_field("humor", 5) is True
    assert dictionary.set_personality_field("bogus", 2) is False
    assert dictionary.personality_profile()["humor"] == 5  # persisted + reloaded
    # the unrelated [pages.x] section survived the block rewrite
    assert "http://x" in toml.read_text()
