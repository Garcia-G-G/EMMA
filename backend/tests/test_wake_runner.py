"""Wake runner tests (Prompt 16.3, D2). The pipeline is mocked — no torch, no
ElevenLabs, no network. We assert the TaskRecord.meta transitions, cost tracking,
and the honest Spanish failure mapping."""

from __future__ import annotations

import errno
import types

import pytest

from backend import wake_runner


# ---- exception stand-ins (match by __name__, as _friendly_error does) -------
class OutOfCreditsError(Exception):
    pass


class TrainingError(Exception):
    pass


class VoiceGenError(Exception):
    pass


class _Ctl:
    task_id = "test123"


def _params(**over):
    p = {
        "phrases": ["hey emma"],
        "negative_phrases": ["hola"],
        "voices_es": 1, "voices_en": 1,
        "n_per_voice": 2, "n_neg_per_voice": 1, "epochs": 3,
        "max_cost_usd": 5.0,
    }
    p.update(over)
    return p


def _fake_modules(monkeypatch, *, generate=None, train=None):
    wd = types.SimpleNamespace(generate=generate or (lambda *a, **k: (0, 0)))
    tw = types.SimpleNamespace(train=train or (lambda *a, **k: {}))
    monkeypatch.setattr(wake_runner, "_load_data_module", lambda: wd)
    monkeypatch.setattr(wake_runner, "_load_train_module", lambda: tw)


# ---- pure helpers -----------------------------------------------------------


def test_estimate_chars_and_cost(monkeypatch):
    monkeypatch.setattr(wake_runner.settings, "WAKE_COST_PER_1K_CHARS", 0.30)
    chars = wake_runner.estimate_chars(["hey emma"], ["hola"], ["v1", "v2"], 2, 1)
    assert chars == 2 * 2 * 8 + 2 * 1 * 4   # voices*npos*len + voices*nneg*len
    assert wake_runner.estimate_cost_usd(1000) == 0.30


def test_resolve_voices_uses_pools_then_fallback(monkeypatch):
    monkeypatch.setattr(wake_runner.settings, "WAKE_VOICE_POOL_ES", "a,b,c")
    monkeypatch.setattr(wake_runner.settings, "WAKE_VOICE_POOL_EN", "x,y")
    assert wake_runner.resolve_voices(2, 1) == ["a", "b", "x"]


def test_eta_is_none_until_meaningful():
    assert wake_runner.eta_seconds(0.0, 0.0) is None
    assert wake_runner.eta_seconds(0.0, 0.5, now=10.0) == 10


# ---- happy path -------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_reports_phase_progress_and_cost(monkeypatch):
    def fake_generate(out, phr, neg, voices, n_pos, n_neg, progress_cb, on_retry):
        progress_cb("positive", 1, 2, 8)
        progress_cb("positive", 2, 2, 16)
        progress_cb("negative", 1, 1, 20)
        return (2, 1)

    def fake_train(data, out, epochs, validate, on_progress):
        on_progress("training", 1, epochs, "Epoch 1/3")
        on_progress("training", epochs, epochs, f"Epoch {epochs}/{epochs}")
        on_progress("validating", 1, 1, "Umbral recomendado: 0.5")
        return {
            "recommended_threshold": 0.5,
            "validation": {"positives_total": 5, "negatives_total": 3,
                           "thresholds": [{"threshold": 0.5, "detected": 4, "false_wakes": 0}]},
        }

    _fake_modules(monkeypatch, generate=fake_generate, train=fake_train)
    monkeypatch.setattr(wake_runner.settings, "WAKE_COST_PER_1K_CHARS", 0.30)
    meta: dict = {}
    rc = await wake_runner._run(_params(), meta, _Ctl())

    assert rc == 0
    assert meta["phase"] == "done"
    assert meta["progress_pct"] == 1.0
    assert meta["samples_generated"] == {"positive": 2, "negative": 1}
    assert meta["cost_so_far_usd"] == pytest.approx(20 * 0.30 / 1000)  # last char count
    assert meta["recommended_threshold"] == 0.5
    assert meta["validation"]["positives_total"] == 5


@pytest.mark.asyncio
async def test_run_surfaces_429_backoff_message(monkeypatch):
    def fake_generate(out, phr, neg, voices, n_pos, n_neg, progress_cb, on_retry):
        on_retry(2.0)  # the script hit a 429 and is waiting
        progress_cb("positive", 1, 1, 8)
        return (1, 0)

    _fake_modules(monkeypatch, generate=fake_generate, train=lambda *a, **k: {"validation": None})
    meta: dict = {}
    await wake_runner._run(_params(), meta, _Ctl())
    # the run did NOT fail on 429 — it continued to done
    assert meta["phase"] == "done"


# ---- failure modes ----------------------------------------------------------


@pytest.mark.asyncio
async def test_run_402_out_of_credits(monkeypatch):
    def boom(*a, **k):
        raise OutOfCreditsError("Sin créditos en ElevenLabs.")

    _fake_modules(monkeypatch, generate=boom)
    meta: dict = {}
    rc = await wake_runner._run(_params(), meta, _Ctl())
    assert rc == 1
    assert meta["phase"] == "failed"
    assert "créditos" in meta["error"]


@pytest.mark.asyncio
async def test_run_training_divergence(monkeypatch):
    def diverge(*a, **k):
        raise TrainingError("El entrenamiento no convergió. Intenta con más muestras o frases distintas.")

    _fake_modules(monkeypatch, generate=lambda *a, **k: (1, 1), train=diverge)
    meta: dict = {}
    await wake_runner._run(_params(), meta, _Ctl())
    assert meta["phase"] == "failed"
    assert "convergió" in meta["error"]


@pytest.mark.asyncio
async def test_run_disk_full(monkeypatch):
    def nospace(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    _fake_modules(monkeypatch, generate=nospace)
    meta: dict = {}
    await wake_runner._run(_params(), meta, _Ctl())
    assert meta["phase"] == "failed"
    assert meta["error"] == "Sin espacio en disco."


@pytest.mark.asyncio
async def test_run_fails_when_no_voices(monkeypatch):
    monkeypatch.setattr(wake_runner, "resolve_voices", lambda a, b: [])
    meta: dict = {}
    rc = await wake_runner._run(_params(), meta, _Ctl())
    assert rc == 1
    assert meta["phase"] == "failed"
