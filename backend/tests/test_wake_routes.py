"""Wake Word Studio route tests (Prompt 16.3, D1). FastAPI TestClient; the
background runner is stubbed to a no-op so no training/ElevenLabs/torch runs.
Registry side effects (macOS notify, tasks.jsonl) are silenced."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import core.background as bg
from backend import auth, db, wake_routes
from backend.app import app
from backend.config import settings
from core.background import registry


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", str(tmp_path / "t.db"))
    db.init_db()
    monkeypatch.setattr(settings, "WAKE_DATA_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr(settings, "WAKE_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(settings, "WAKE_DAEMON_ENV_FILE", str(tmp_path / ".env"))
    # don't pop a real notification or write the real tasks.jsonl when bg jobs end
    async def _silent(*a, **k):
        return None
    monkeypatch.setattr(bg, "_notify_macos", _silent)
    monkeypatch.setattr(registry(), "_db", tmp_path / "tasks.jsonl")

    # stub the runner so "start a job" doesn't actually train
    async def _noop_run(controller, params):
        return 0
    monkeypatch.setattr(wake_routes.wake_runner, "run_job", _noop_run)


@pytest.fixture
def client():
    return TestClient(app)


def _auth_off(monkeypatch):
    monkeypatch.setattr(wake_routes.settings, "WAKE_STUDIO_REQUIRE_AUTH", False)


def _login(client):
    user = db.upsert_user("garcia@example.com", "Garcia", "google", "pid-1")
    client.cookies.set("emma_session", auth._serializer.dumps({"uid": user["id"]}))


# ---- validation -------------------------------------------------------------


def test_create_rejects_phrase_over_40_chars(client, monkeypatch):
    _auth_off(monkeypatch)
    r = client.post("/wake/jobs", json={"phrases": ["x" * 41]})
    assert r.status_code == 422


def test_create_rejects_empty_phrases(client, monkeypatch):
    _auth_off(monkeypatch)
    r = client.post("/wake/jobs", json={"phrases": []})
    assert r.status_code == 422


def test_create_rejects_cost_over_user_cap(client, monkeypatch):
    _auth_off(monkeypatch)
    # tiny cap vs the (now ceiling-clamped) max samples → estimated cost over cap → 400
    r = client.post("/wake/jobs", json={"phrases": ["hey emma"], "n_per_voice": 500, "max_cost_usd": 0.0001})
    assert r.status_code == 400
    assert "supera" in r.json()["detail"]


# ---- create + read ----------------------------------------------------------


def test_create_returns_job_id_and_stream_url(client, monkeypatch):
    _auth_off(monkeypatch)
    r = client.post("/wake/jobs", json={"phrases": ["Hey Emma"]})
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"]
    assert body["stream_url"] == f"/wake/jobs/{body['job_id']}/stream"
    assert "estimated_cost_usd" in body


def test_get_job_returns_expected_shape(client, monkeypatch):
    _auth_off(monkeypatch)
    jid = client.post("/wake/jobs", json={"phrases": ["Hey Emma"]}).json()["job_id"]
    r = client.get(f"/wake/jobs/{jid}")
    assert r.status_code == 200
    d = r.json()
    for key in ("id", "phase", "progress_pct", "message", "cost_so_far_usd",
                "samples_generated", "started_at", "eta_seconds", "error"):
        assert key in d


def test_get_unknown_job_404(client, monkeypatch):
    _auth_off(monkeypatch)
    assert client.get("/wake/jobs/nope").status_code == 404


# ---- SSE --------------------------------------------------------------------


def test_stream_emits_terminal_event(client, monkeypatch):
    _auth_off(monkeypatch)
    jid = client.post("/wake/jobs", json={"phrases": ["Hey Emma"]}).json()["job_id"]
    # drive the job to a terminal phase so the SSE generator emits once and closes
    rec = registry().get(jid)
    rec.meta.update(phase="done", progress_pct=1.0, message="Listo.",
                    recommended_threshold=0.5,
                    validation={"positives_total": 5, "negatives_total": 3, "thresholds": []})
    r = client.get(f"/wake/jobs/{jid}/stream")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "data:" in r.text and '"phase": "done"' in r.text


# ---- delete -----------------------------------------------------------------


def test_delete_cancels_and_removes_partial_output(client, monkeypatch, tmp_path):
    _auth_off(monkeypatch)
    jid = client.post("/wake/jobs", json={"phrases": ["Hey Emma"]}).json()["job_id"]
    job_dir = tmp_path / "jobs" / jid
    job_dir.mkdir(parents=True)
    (job_dir / "partial.wav").write_bytes(b"x")
    r = client.delete(f"/wake/jobs/{jid}")
    assert r.status_code == 200 and r.json()["cancelled"] is True
    assert not job_dir.exists()
    assert registry().get(jid).meta["phase"] == "cancelled"


# ---- install ----------------------------------------------------------------


def test_install_copies_model_and_updates_env(client, monkeypatch, tmp_path):
    _auth_off(monkeypatch)
    jid = client.post("/wake/jobs", json={"phrases": ["Hey Emma"]}).json()["job_id"]
    model = tmp_path / "src.onnx"
    model.write_bytes(b"onnx-bytes")
    rec = registry().get(jid)
    rec.meta.update(phase="done", model_path=str(model), slug="hey_emma", recommended_threshold=0.6)

    r = client.post(f"/wake/jobs/{jid}/install")
    assert r.status_code == 200
    body = r.json()
    assert body["installed"] is True and body["active_model"] == "hey_emma.onnx"
    assert (tmp_path / "models" / "hey_emma.onnx").read_bytes() == b"onnx-bytes"
    env_text = (tmp_path / ".env").read_text()
    assert "WAKE_WORD_PATH=" in env_text and "WAKE_WORD_NAME=hey_emma" in env_text


def test_install_409_when_not_done(client, monkeypatch):
    _auth_off(monkeypatch)
    jid = client.post("/wake/jobs", json={"phrases": ["Hey Emma"]}).json()["job_id"]
    assert client.post(f"/wake/jobs/{jid}/install").status_code == 409


# ---- auth gate --------------------------------------------------------------


def test_auth_gate_blocks_when_required(client, monkeypatch):
    monkeypatch.setattr(wake_routes.settings, "WAKE_STUDIO_REQUIRE_AUTH", True)
    assert client.post("/wake/jobs", json={"phrases": ["Hey Emma"]}).status_code == 401


def test_auth_gate_allows_logged_in_user(client, monkeypatch):
    monkeypatch.setattr(wake_routes.settings, "WAKE_STUDIO_REQUIRE_AUTH", True)
    _login(client)
    assert client.post("/wake/jobs", json={"phrases": ["Hey Emma"]}).status_code == 200


def test_config_reports_auth_state(client, monkeypatch):
    monkeypatch.setattr(wake_routes.settings, "WAKE_STUDIO_REQUIRE_AUTH", True)
    d = client.get("/wake/config").json()
    assert d["require_auth"] is True and d["authenticated"] is False
    assert "cost_per_1k_chars" in d


def test_page_serves_html(client):
    r = client.get("/wake")
    assert r.status_code == 200 and "Wake Word Studio" in r.text


def test_epochs_and_samples_are_clamped(client, monkeypatch):
    # 24.6 audit: ceilings prevent resource-exhaustion (epochs escapes the cost guard).
    _auth_off(monkeypatch)
    from backend.wake_routes import WakeJobRequest
    req = WakeJobRequest(phrases=["hey emma"], epochs=10_000_000, n_per_voice=10_000_000,
                         voices_es=999, voices_en=999)
    assert req.epochs == 200
    assert req.n_per_voice == 500
    assert req.voices_es == 20 and req.voices_en == 20


def test_concurrent_job_cap_returns_429(client, monkeypatch):
    # 24.6 audit: too many running wake_train jobs → 429, no new job.
    _auth_off(monkeypatch)
    from backend.wake_routes import _KIND, _MAX_CONCURRENT_WAKE_JOBS
    from core.background import registry

    class _Rec:
        def __init__(self):
            self.kind = _KIND
            self.status = "running"
            self.meta = {"phase": "training"}
    monkeypatch.setattr(registry(), "list",
                        lambda **k: [_Rec() for _ in range(_MAX_CONCURRENT_WAKE_JOBS)])
    r = client.post("/wake/jobs", json={"phrases": ["hey emma"]})
    assert r.status_code == 429
