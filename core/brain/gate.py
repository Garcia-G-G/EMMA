"""The gate: decide local, or defer — and record why (BRAIN-1 Part 5).

A pure function of the retrieval, the normalized text and plain tool
metadata. Checks run in the order of CORE-LOCAL-ARCHITECTURE §3a; every check
is recorded as a ``Feature`` with its threshold, whether it decided or not, so
thresholds get tuned from logged data. **Fail closed, say why**: every
non-local verdict carries a reason.

Thresholds are data (``Thresholds``), built by the caller from the ``BRAIN_*``
settings — the brain never imports settings.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from core.brain.decision import Candidate, Decision, Feature, Verdict
from core.brain.normalize import Normalized, fold
from core.brain.retrieve import NONE_TOOL, PSEUDO_TOOLS, Retrieval, ToolDoc
from core.brain.sinks import PERSIST, is_sink
from core.brain.slots import FILLER, STOP, TOOLS, Vocab, resolve


@dataclass(frozen=True)
class Thresholds:
    knn_min: float = 0.85  # τ: cosine to the nearest example (novelty floor)
    margin_min: float = 0.15  # δ: top-1 vs top-2, on both sources
    max_content_tokens: int = 12
    slot_floor: float = 0.85
    # BRAIN-1.5 gate study. How step 3 may pick a tool besides strict agreement:
    relaxed_agreement: bool = False  # B: kNN top-1 in BM25 top-3, or BM25 top-1 in kNN top-3
    bm25_single_min: float | None = None  # C: θ1, BM25 top-1 score alone is enough …
    bm25_single_gap: float = 0.0  # … with this relative gap to BM25 top-2 (δ1)
    knn_single_min: float | None = None  # C: θ2, kNN top-1 cosine alone is enough
    # Study only — never a setting: what the argument check is worth on its own.
    require_slots: bool = True


# ---- closed phrase lists (both languages, neutral) ------------------------------------

# Things STT emits on silence or noise. Closed: a model that "looks like noise"
# is exactly what a real short request can be mistaken for.
_NOISE_PHRASES = tuple(fold(p) for p in (
    "subtitulos por la comunidad de amara.org", "subtitulos realizados por la comunidad",
    "gracias por ver", "suscribete", "thanks for watching", "microsoft word",
    "msworddoc",
))
# Requests Emma refuses outright, whatever tool might match.
_REFUSE = (
    # destroy the home folder / the disk
    (r"\b(borra|elimina|delete|wipe|formatea)\b.*\b(home|carpeta personal|disco|todo mi mac)\b",
     "destroy_home"),
    (r"\brm\s*-?\s*rf\b|\berre eme menos erre efe\b", "rm_rf"),
    # read a secret aloud / hand a secret to someone
    (r"\b(dime|di|leeme|lee|cual es|tell me|what is|read)\b.*\b(token|contrasena|password|clave|"
     r"api key|pin)\b", "secret_aloud"),
    (r"\b(manda|mandale|envia|enviale|send|comparte)\b.*\b(contrasena|password|token|clave)\b",
     "secret_out"),
    # hand over the machine
    (r"\b(control total|total control|acceso total|full access)\b", "total_control"),
)
# Capabilities Emma doesn't have: say so instead of guessing a neighbor.
# Anchored: a call request OPENS with the verb. "recuérdame … llamar al dentista"
# is a reminder whose text mentions a call, not a call.
_UNSUPPORTED = ((r"^(llama|llamale|llamar|marcale|call)\b\s+(a\s+)?\S+", "phone_call"),)
# An action with no object: ask what, don't guess.
_VAGUE_VERBS = frozenset(fold(w) for w in (
    "busca", "buscame", "manda", "mandale", "envia", "enviale", "cierra", "search", "send",
    "close", "pon", "ponme", "abre",
))
_GENERIC_OBJECTS = frozenset(fold(w) for w in (
    "algo", "todo", "mensaje", "repo", "cosa", "something", "everything", "message",
    "thing", "anything",
))
# Small talk: answered from a template, no tool, no model.
_CHITCHAT = tuple(fold(p) for p in (
    "como estas", "que haces", "que eres", "de que pais eres", "que lengua tienes",
    "que acento tienes", "gracias", "chao", "adios", "no pasa nada", "da igual",
    "estamos bien", "te pregunte", "how are you", "thank you", "thanks", "bye",
    "what are you",
))
_COMPLEX_MARKERS = tuple(fold(p) for p in (
    "y luego", "y despues", "despues", "luego", "and then", "after that", "ademas",
))
_ANAPHORA = frozenset({"eso", "esa", "ese", "esto", "ella", "el que", "it", "that", "those"})
# "…y dime qué encontraste": a conjunction then a SECOND imperative is two
# requests, even without "luego". Closed verb list, both languages.
_SECOND_VERB = frozenset(fold(v) for v in (
    "dime", "di", "abre", "pon", "busca", "cierra", "crea", "borra", "manda", "mandale",
    "haz", "hazlo", "agrega", "apunta", "lee", "leeme", "ejecuta", "corre", "muestrame",
    "ensename", "postealo", "publica", "check", "tell", "open", "play", "search", "close",
    "create", "delete", "send", "run", "show", "add", "read",
))


# A question asks; it never tells Emma something to keep. "¿Puedes decirme qué
# repositorios tiene el usuario X?" agreed on remember_user_profile and filled
# `value` with the whole question (log_11, BRAIN-1.5) — a store from a question.
_QUESTION_OPENERS = frozenset(fold(w) for w in (
    "que", "cual", "cuales", "cuanto", "cuantos", "cuantas", "donde", "como", "quien", "quienes",
    "cuando", "puedes", "podrias", "sabes", "what", "which", "how", "where", "who", "when",
    "can", "could", "do", "does", "is", "are",
))


def _is_question(norm: Normalized) -> bool:
    if "?" in norm.original or "¿" in norm.original:
        return True
    return bool(norm.tokens) and norm.tokens[0] in _QUESTION_OPENERS


def _conj_then_verb(tokens: tuple[str, ...]) -> str | None:
    for i, t in enumerate(tokens[:-1]):
        if t in ("y", "and", "e") and tokens[i + 1] in _SECOND_VERB:
            return f"{t} {tokens[i + 1]}"
    return None


def _matches_phrase(tokens: tuple[str, ...], phrase: str) -> bool:
    return (" " + phrase + " ") in (" " + " ".join(tokens) + " ")


def _content(tokens: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(t for t in tokens if t not in STOP and t not in FILLER)


def _decision(
    norm: Normalized,
    verdict: Verdict,
    reason: str,
    features: list[Feature],
    retrieval: Retrieval | None = None,
    **kw: object,
) -> Decision:
    return Decision(
        utterance=norm.original,
        normalized=norm.text,
        verdict=verdict,
        reason=reason,
        candidates=retrieval.candidates if retrieval else (),
        features=tuple(features),
        **kw,  # type: ignore[arg-type]
    )


def pre_retrieval(norm: Normalized) -> tuple[Verdict, str] | None:
    """Steps 1-2: noise, refusals, unsupported, vague, small talk. No retrieval needed."""
    toks = norm.tokens
    content = _content(toks)
    if not content:
        return "none", "empty"
    folded = " ".join(toks)
    if any(_matches_phrase(toks, " ".join(p.split())) or p in norm.folded for p in _NOISE_PHRASES):
        return "none", "noise:stt_artifact"
    if len(set(content)) == 1 and len(content) >= 3:
        return "none", "noise:repetition"
    for pat, why in _REFUSE:
        if re.search(pat, folded):
            return "refuse", f"safety:{why}"
    for pat, why in _UNSUPPORTED:
        if re.search(pat, folded):
            return "refuse", f"unsupported:{why}"
    if content[0] in _VAGUE_VERBS and all(t in _GENERIC_OBJECTS for t in content[1:]) and content[1:]:
        return "clarify", "vague:no_object"
    if all(any(t in p.split() for p in _CHITCHAT) for t in content):
        return "none", "smalltalk"
    return None


def decide(
    norm: Normalized,
    retrieval: Retrieval,
    tools: Mapping[str, ToolDoc],
    vocab: Vocab,
    th: Thresholds,
    knn_status: str = "ok",
) -> Decision:
    f: list[Feature] = []
    content = _content(norm.tokens)
    f.append(Feature("content_tokens", float(len(content))))

    # 1-2. closed phrase lists
    early = pre_retrieval(norm)
    f.append(Feature("pre_retrieval", early[1] if early else "pass"))
    if early:
        return _decision(norm, early[0], early[1], f, retrieval)
    # A single word nobody's frame or vocabulary explains is STT noise ("KMO", "Hmm").
    if len(content) == 1 and not _explained_single(content[0], tools, vocab):
        f.append(Feature("single_unexplained_token", content[0]))
        return _decision(norm, "none", "noise:single_token", f, retrieval)

    if knn_status != "ok":
        # Fail closed and say why: without the embedder (a fresh install whose
        # model download failed) one retriever can't vote, so nothing goes local.
        f.append(Feature("knn_status", knn_status, passed=False))
        return _decision(norm, "defer", knn_status, f, retrieval)

    cands = retrieval.candidates
    bm_sorted = sorted((c for c in cands if c.bm25_rank), key=lambda c: c.bm25_rank)
    kn_sorted = sorted((c for c in cands if c.knn_rank), key=lambda c: c.knn_rank)
    bm1 = bm_sorted[0].tool if bm_sorted else None
    kn1 = kn_sorted[0].tool if kn_sorted else None

    # 3. choose a tool: agreement, or (study variants) relaxed / single-source
    chosen, how = _choose(bm_sorted, kn_sorted, th)
    f.append(Feature("bm25_top1", bm1 or ""))
    f.append(Feature("knn_top1", kn1 or ""))
    f.append(Feature("agree", how == "agree", passed=chosen is not None))
    f.append(Feature("choice_path", how))
    if chosen is None:
        return _decision(norm, "defer", "disagree" if how in ("none", "disagree") else how, f,
                         retrieval)
    if chosen not in tools:
        # The index knows every tool; this machine may not have this one.
        return _decision(norm, "defer", "unavailable", f, retrieval, chosen=chosen)
    if chosen == NONE_TOOL:
        # The nearest examples wanted no tool — but declining a real request is a
        # wrong answer, and kNN alone can't tell. Ask the LLM.
        return _decision(norm, "defer", "knn_none", f, retrieval)

    # 4. kNN distance — of the CHOSEN tool. A pick BM25 made alone is BM25's call.
    cos = next((c.knn_score for c in kn_sorted if c.tool == chosen), 0.0)
    if how == "single:bm25":
        f.append(Feature("knn_cosine", cos, threshold=th.knn_min, passed=None))
    else:
        ok_cos = cos >= th.knn_min
        f.append(Feature("knn_cosine", cos, threshold=th.knn_min, passed=ok_cos))
        if not ok_cos:
            return _decision(norm, "defer", "novel", f, retrieval, chosen=chosen)

    # 5. margin, both sources
    bm_margin = _rel_margin(bm_sorted)
    kn_margin = _abs_margin(kn_sorted)
    ok_m = bm_margin >= th.margin_min and kn_margin >= th.margin_min
    f.append(Feature("bm25_margin", round(bm_margin, 4), threshold=th.margin_min,
                     passed=bm_margin >= th.margin_min))
    f.append(Feature("knn_margin", round(kn_margin, 4), threshold=th.margin_min,
                     passed=kn_margin >= th.margin_min))
    if not ok_m:
        return _decision(norm, "defer", "ambiguous", f, retrieval, chosen=chosen)

    # 6. multi-step / long
    marker = next((m for m in _COMPLEX_MARKERS if _matches_phrase(norm.tokens, m)), None)
    marker = marker or _conj_then_verb(norm.tokens)
    too_long = len(content) > th.max_content_tokens
    f.append(Feature("complex_marker", marker or "", passed=marker is None))
    f.append(Feature("content_tokens_ok", not too_long, threshold=float(th.max_content_tokens),
                     passed=not too_long))
    if marker or too_long:
        return _decision(norm, "defer", "complex", f, retrieval, chosen=chosen)

    # 7. slots: every required argument, nothing unexplained, no dangling pronoun
    doc = tools.get(chosen)
    required = doc.required if doc else ()
    res = resolve(chosen, norm, vocab, required=required, floor=th.slot_floor)
    anaphora = [t for t in res.leftover if t in _ANAPHORA]
    f.append(Feature("slots_missing", ",".join(res.missing), passed=not res.missing))
    f.append(Feature("leftover", ",".join(res.leftover), passed=not res.leftover))
    if anaphora:
        return _decision(norm, "defer", "anaphora", f, retrieval, chosen=chosen, slots=res.slots)
    stores = chosen in PERSIST
    asks = _is_question(norm)
    f.append(Feature("question_into_store", stores and asks, passed=not (stores and asks)))
    if stores and asks:
        return _decision(norm, "defer", "question_not_statement", f, retrieval, chosen=chosen,
                         slots=res.slots)
    if th.require_slots and (res.missing or res.leftover or chosen not in TOOLS):
        return _decision(norm, "defer", "slots", f, retrieval, chosen=chosen, slots=res.slots)

    # 8. risk class: local, but the confirmation flow runs first
    risky = bool(doc and doc.destructive) or is_sink(chosen)
    f.append(Feature("needs_confirmation", risky))
    return _decision(
        norm, "local", "ok", f, retrieval, chosen=chosen, slots=res.slots,
        needs_confirmation=risky,
    )


def _choose(
    bm: list[Candidate], kn: list[Candidate], th: Thresholds
) -> tuple[str | None, str]:
    """(tool, how). ``how``: agree | relaxed:* | single:* | conflict | disagree | none."""
    bm1 = bm[0].tool if bm else None
    kn1 = kn[0].tool if kn else None
    if bm1 is None and kn1 is None:
        return None, "none"
    if bm1 is not None and bm1 == kn1:
        return bm1, "agree"
    if kn1 in PSEUDO_TOOLS:
        # The nearest examples say "no tool" or "needs the LLM": no relaxed or
        # single-source rule may talk over that.
        return None, "knn_none" if kn1 == NONE_TOOL else "knn_llm"
    if th.relaxed_agreement:
        if kn1 is not None and kn1 in [c.tool for c in bm[:3]]:
            return kn1, "relaxed:knn_in_bm25_top3"
        if bm1 is not None and bm1 in [c.tool for c in kn[:3]]:
            return bm1, "relaxed:bm25_in_knn_top3"
    single_bm = (
        bm1 if th.bm25_single_min is not None and bm
        and bm[0].bm25_score >= th.bm25_single_min and _rel_margin(bm) >= th.bm25_single_gap
        else None
    )
    single_kn = (
        kn1 if th.knn_single_min is not None and kn and kn[0].knn_score >= th.knn_single_min
        else None
    )
    if single_bm and single_kn and single_bm != single_kn:
        return None, "conflict"
    if single_kn:
        return single_kn, "single:knn"
    if single_bm:
        return single_bm, "single:bm25"
    return None, "disagree"


def _rel_margin(ranked: list[Candidate]) -> float:
    if not ranked:
        return 0.0
    s1 = ranked[0].bm25_score
    s2 = ranked[1].bm25_score if len(ranked) > 1 else 0.0
    return (s1 - s2) / s1 if s1 > 0 else 0.0


def _abs_margin(ranked: list[Candidate]) -> float:
    if not ranked:
        return 0.0
    c1 = ranked[0].knn_score
    c2 = next((c.knn_score for c in ranked[1:] if c.tool != NONE_TOOL), 0.0)
    return c1 - c2


def _explained_single(token: str, tools: Mapping[str, ToolDoc], vocab: Vocab) -> bool:
    if token in vocab.aliases or any(fold(a) == token for a in vocab.apps):
        return True
    return any(token in spec.frames or token in (" ".join(spec.frames)).split()
               for spec in TOOLS.values())
