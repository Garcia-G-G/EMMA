"""Prompt 35.1 — voice_profiles store (enroll/identify/cosine, no resemblyzer needed)."""

from __future__ import annotations

import numpy as np
import pytest

from config.settings import settings
from memory import voice_profiles as vp


@pytest.fixture(autouse=True)
def _tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_DB_PATH", tmp_path / "memory.db")
    yield


def _emb(seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(256).astype(np.float32).tobytes()


def test_schema_init_idempotent() -> None:
    vp.init()
    vp.init()  # second time must not raise
    assert vp.count_sync() == 0


@pytest.mark.asyncio
async def test_enroll_then_identify_roundtrip() -> None:
    e = _emb(1)
    await vp.enroll("garcia", e)
    assert vp.count_sync() == 1
    assert await vp.identify(e) == "garcia"  # same embedding → cosine 1.0


@pytest.mark.asyncio
async def test_too_different_identifies_none() -> None:
    await vp.enroll("garcia", _emb(1))
    assert await vp.identify(_emb(999)) is None  # unrelated voice → below threshold


@pytest.mark.asyncio
async def test_reenroll_bumps_confidence() -> None:
    await vp.enroll("garcia", _emb(1))
    await vp.enroll("garcia", _emb(2))
    profs = await vp.list_profiles()
    assert len(profs) == 1 and profs[0].confidence > 1.0


@pytest.mark.asyncio
async def test_mark_seen_and_delete() -> None:
    await vp.enroll("garcia", _emb(1))
    await vp.mark_seen("garcia")
    assert (await vp.list_profiles())[0].last_seen is not None
    assert await vp.delete_profile("garcia") is True
    assert await vp.list_profiles() == []


def test_cosine_bounds() -> None:
    v = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    assert vp._cosine(v, v) == pytest.approx(1.0)
    assert vp._cosine(v, np.array([0.0, 1.0, 0.0], dtype=np.float32)) == pytest.approx(0.0)
