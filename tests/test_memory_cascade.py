"""Step 42 Part 4 — memory on the cascade, end to end, nothing leaving the Mac.

Real brain (live tools + memory's route), real processor, the cascade's Host and
the injection guard, a temp memory.db. Every socket connect fails the test.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from core import cascade, injection, runtime, session_memory
from core.brain.brain import Brain
from core.brain.embed import Embedder
from core.brain.processor import BrainProcessor
from core.brain.slots import Vocab
from core.brain_host import build_index, thresholds_from_settings, tool_docs
from memory import long_term, structured
from memory.structured import route
from tests.test_brain_processor import _say, _user
from tools import memory_tool

EMBEDDER = Embedder()
pytestmark = pytest.mark.skipif(not EMBEDDER.available(), reason="local embedder missing")


@pytest.fixture(scope="module")
def brain(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Brain]:
    index = build_index(EMBEDDER, tmp_path_factory.mktemp("brain") / "brain.db")
    yield Brain(tool_docs(available_only=True), Vocab(), thresholds_from_settings(), index,
                EMBEDDER, route=route)
    index.close()


@pytest.fixture
def mem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(long_term.settings, "MEMORY_DB_PATH", tmp_path / "memory.db")

    def offline(*a: object, **k: object) -> None:
        raise AssertionError("memory opened a network connection")

    monkeypatch.setattr(socket.socket, "connect", offline)
    session_memory.clear()
    injection.install()
    runtime.set_spoken_lang("es")
    yield
    injection.uninstall()
    session_memory.clear()


def _dog() -> list[str]:
    with long_term._connect() as conn:
        return structured.lookup(conn, "dog", "name")


async def _turn(proc: BrainProcessor, text: str) -> str:
    _user(text)
    said = await _say(proc, text)
    await asyncio.sleep(0.01)
    return said[0] if said else ""


@pytest.mark.asyncio
async def test_remember_ask_yes_recall_change_no_forget(brain: Brain, mem: None) -> None:
    proc = BrainProcessor(lambda: brain.decide, host=cascade._Host())
    assert await _turn(proc, "Recuerda que mi perro se llama Toby.") == "¿Guardo que tu perro se llama Toby?"
    assert _dog() == []  # nothing written before the yes
    assert "Toby" in await _turn(proc, "sí")
    assert _dog() == ["Toby"]
    assert await _turn(proc, "¿Cómo se llama mi perro?") == "Tu perro se llama Toby."
    assert await _turn(proc, "Recuerda que mi perro se llama Max.") == \
        "Tenía que tu perro se llama Toby. ¿Lo cambio a Max?"
    await _turn(proc, "no")
    assert _dog() == ["Toby"]  # declined: unchanged
    assert "Tu perro se llama Toby" in await _turn(proc, "¿Qué sabes de mí?")
    assert await _turn(proc, "Olvida que mi perro se llama Toby.") == "¿Olvido que tu perro se llama Toby?"
    await _turn(proc, "sí")
    assert _dog() == []
    assert await _turn(proc, "¿Cómo se llama mi perro?") == "No tengo eso guardado."


@pytest.mark.asyncio
async def test_english_and_a_value_never_given_is_not_invented(brain: Brain, mem: None) -> None:
    proc = BrainProcessor(lambda: brain.decide, host=cascade._Host())
    assert await _turn(proc, "Remember that my favorite color is green.") == \
        "Save that your favorite color is green?"
    await _turn(proc, "yes")
    assert await _turn(proc, "What's my favorite color?") == "Your favorite color is green."
    assert await _turn(proc, "What's my favorite song?") == "I don't have that saved."


@pytest.mark.asyncio
async def test_a_secret_is_refused_and_an_unconfirmed_call_writes_nothing(mem: None) -> None:
    r = await memory_tool.remember_fact("mi contraseña del banco es hunter2", confirmed=True)
    assert not r.success and "secreto" in r.user_message
    r = await memory_tool.remember_fact("mi gato se llama Nala")
    assert r.requires_confirmation and not r.success
    r = await memory_tool.remember_fact("el técnico viene el martes")
    assert r.requires_confirmation and not r.success
    with long_term._connect() as conn:
        assert structured.all_active(conn) == []
    assert await long_term.count() == 0
