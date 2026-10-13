"""BRAIN-1 Part 1 — normalization and the decision log.

Normalization cases are real utterances from the labelled set (ids in comments),
not invented ones.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from core.brain import log as brain_log
from core.brain.decision import Candidate, Decision, Feature, SlotValue
from core.brain.normalize import fold, normalize


@pytest.mark.parametrize(
    ("utterance", "text"),
    [
        ("Hey Emma, sube el volumen a 70.", "sube el volumen a 70"),  # sc_A03
        ("AYEMA ABRE MI GITCHUB", "abre mi gitchub"),  # log_04
        ("AIMA ABRE SPOTIFY", "abre spotify"),  # log_03
        ("Ay, Emma, cierra esta pestaña.", "cierra esta pestaña"),  # log_06
        ("ey emma borra mi nota compras", "borra mi nota compras"),  # log_02
        ("¿Cómo estás? Te pregunté, Emma.", "cómo estás? te pregunté"),  # log_35
        ("Hey Emma, ¿qué hora es en Tokio?", "qué hora es en tokio?"),  # sc_A05
    ],
)
def test_wake_and_vocative_are_removed(utterance: str, text: str) -> None:
    assert normalize(utterance).text == text


def test_hola_inside_dictated_text_survives() -> None:
    # log_21: only a LEADING wake run is stripped; "hola" here is the post's text.
    n = normalize("Tu que hagas un tuit en Nex de que sea que diga hola probando Emma")
    assert "hola probando" in n.text
    assert "emma" not in n.tokens


def test_accents_fold_for_matching_only() -> None:
    n = normalize("Hey Emma, cierra esta pestaña.")
    assert n.text == "cierra esta pestaña"  # display keeps the ñ
    assert n.folded == "cierra esta pestana"
    assert n.tokens == ("cierra", "esta", "pestana")
    assert fold("Música Él ÑANDÚ") == "musica el nandu"


def test_empty_and_wake_only() -> None:
    assert normalize("").tokens == ()
    assert normalize("Hey Emma").tokens == ()


def _decision() -> Decision:
    return Decision(
        utterance="Hey Emma, sube el volumen a 70.",
        normalized="sube el volumen a 70",
        verdict="defer",
        reason="skeleton",
        candidates=(Candidate(tool="set_volume", bm25_score=9.1, bm25_rank=1),),
        chosen="set_volume",
        slots={"percent": SlotValue(70, "regex:number", 1.0, "70")},
        features=(Feature("knn_cosine", 0.91, threshold=0.85, passed=True),),
        timings={"total_ms": 3.2},
    )


def test_log_writes_one_line_with_features_and_slots(tmp_path: Path) -> None:
    p = tmp_path / "brain_decisions.jsonl"
    brain_log.write(_decision(), path=p, extra={"realtime_tool": None})
    brain_log.write(_decision(), path=p)
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["slots"]["percent"]["value"] == 70
    assert rec["features"][0] == {
        "name": "knn_cosine", "value": 0.91, "threshold": 0.85, "passed": True
    }
    assert rec["candidates"][0]["knn_rank"] == 0  # absent source = 0, never missing
    assert "realtime_tool" in rec
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert brain_log.read(p)[1]["verdict"] == "defer"


def test_log_redacts_secrets_before_disk(tmp_path: Path) -> None:
    p = tmp_path / "brain_decisions.jsonl"
    key = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    d = _decision()
    d.utterance = f"guarda mi clave {key}"
    brain_log.write(d, path=p)
    assert key not in p.read_text(encoding="utf-8")


def test_log_rotates(tmp_path: Path) -> None:
    p = tmp_path / "brain_decisions.jsonl"
    for _ in range(5):
        brain_log.write(_decision(), path=p, rotate_bytes=1200)
    assert (tmp_path / "brain_decisions.jsonl.1").exists()
    assert p.stat().st_size <= 1200
