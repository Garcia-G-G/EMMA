"""Apple Notes via AppleScript: list, search, create, append, read, merge, delete.

Bug 19.2 reshaped this module:
- B2: ``delete_note`` no longer ``repeat … delete`` over every match (which
  silently deleted ALL same-titled notes and crashed with -1728 mid-loop). It
  enumerates matches by ``id`` + modification date, disambiguates, and deletes
  exactly one by ``id`` via ``delete (note id "…")`` (verified live).
- B4: ``list_notes`` now carries ISO modification dates + a longer preview, and
  two new tools exist — ``read_note`` (returns the body) and ``merge_notes``.

AppleScript facts pinned during 19.2 (macOS Notes, iCloud account):
- ``delete (note id "<id>")`` moves exactly one note to "Recently Deleted".
- ``notes whose name is "X"`` spans every folder, INCLUDING Recently Deleted.
- ``before`` / ``after`` are reserved words — never use them as variables.
- A note's title (``name``) is the first line of its ``body``.
"""

from __future__ import annotations

from dataclasses import asdict

import structlog

from actions import macos
from core import dictionary
from memory import episodic
from tools.base import ToolResult, tool
from tools.disambiguation import (
    FIELD_SEP,
    ISO_DATE_HANDLER,
    Match,
    disambiguate,
    find_by_title,
    normalize_title,
    parse_matches,
    suffix_prompt,
    word_common_prefix,
)

log = structlog.get_logger("emma.tools.notes")

_NOTES_TIMEOUT_S = 15.0

# Takes the already-resolved plaintext STRING (computed inside the tell-block by
# the caller): a top-level handler runs outside the app context, so accessing
# `plaintext of nt` in here would silently fail. Joins body paragraphs 2-5
# (skipping the title line) into a single ≤120-char preview.
_PREVIEW_HANDLER = (
    "on previewText(p)\n"
    '  set t to ""\n'
    "  repeat with i from 2 to 5\n"
    "    try\n"
    '      set t to t & (paragraph i of p) & " "\n'
    "    end try\n"
    "  end repeat\n"
    "  if (length of t) > 120 then set t to text 1 thru 120 of t\n"
    "  return t\n"
    "end previewText\n"
)


# Recently-Deleted folder names to skip during enumeration (the user's locales:
# English + Mexican Spanish). A note moved here by `delete` must NOT resurface as
# a live match — otherwise read_note/delete_note re-offer a phantom (Bug 19.2).
# Top-level folders only; deeply-nested subfolders are out of scope.
_TRASH_FOLDERS = '{"Recently Deleted", "Eliminados recientemente", "Eliminados Recientemente"}'


def _enumerate_script(match_clause: str, limit: int) -> str:
    """Build an enumeration script returning ``id‖iso‖title‖preview`` per match.

    ``match_clause`` is the AppleScript predicate applied to ``notes of f``, e.g.
    ``' whose name is "X"'`` or ``''`` (all). We iterate folders so we can skip
    "Recently Deleted". Never deletes — enumeration only (the B2 contract).
    """
    return (
        ISO_DATE_HANDLER + _PREVIEW_HANDLER + 'tell application "Notes"\n'
        '  set out to ""\n'
        "  set k to 0\n"
        "  repeat with f in folders\n"
        f"    if (name of f) is not in {_TRASH_FOLDERS} then\n"
        f"      repeat with nt in (notes of f{match_clause})\n"
        '        set pv to ""\n'
        "        try\n"
        "          set pv to my previewText(plaintext of nt)\n"
        "        end try\n"
        "        set out to out & (id of nt) & "
        f'"{FIELD_SEP}" & (my isoDate(modification date of nt)) & '
        f'"{FIELD_SEP}" & (name of nt) & "{FIELD_SEP}" & pv & linefeed\n'
        "        set k to k + 1\n"
        f"        if k ≥ {int(limit)} then exit repeat\n"
        "      end repeat\n"
        "    end if\n"
        f"    if k ≥ {int(limit)} then exit repeat\n"
        "  end repeat\n"
        "  return out\n"
        "end tell"
    )


async def _enumerate(match_clause: str, limit: int) -> list[Match]:
    raw = await macos.osascript(_enumerate_script(match_clause, limit), timeout_s=_NOTES_TIMEOUT_S)
    return parse_matches(raw)


async def _enumerate_by_title(title_esc: str, limit: int) -> list[Match]:
    """Find notes by title via the shared tiered strategy (exact → starts-with →
    contains). Lets read/delete/merge resolve legacy flattened-title notes;
    disambiguation gates any extra matches it surfaces (Bug 19.5 converges this
    onto find_by_title so the match strategy lives in one place)."""
    matches, _strategy = await find_by_title(_enumerate, title_esc, limit=limit)
    return matches


def _enumerate_light_script(scan_cap: int) -> str:
    """Cheap enumeration: id‖iso-mod-date‖title only — NO plaintext reads.

    Notes' AppleScript dictionary has no sort verb (19.6-B21 deviation from
    the spec's assumption), so finding "la última nota" scans these three
    cheap fields and picks the max in Python. Bodies are never touched."""
    return (
        ISO_DATE_HANDLER + 'tell application "Notes"\n'
        '  set out to ""\n'
        "  set k to 0\n"
        "  repeat with f in folders\n"
        f"    if (name of f) is not in {_TRASH_FOLDERS} then\n"
        "      repeat with nt in (notes of f)\n"
        "        set out to out & (id of nt) & "
        f'"{FIELD_SEP}" & (my isoDate(modification date of nt)) & '
        f'"{FIELD_SEP}" & (name of nt) & linefeed\n'
        "        set k to k + 1\n"
        f"        if k ≥ {int(scan_cap)} then exit repeat\n"
        "      end repeat\n"
        "    end if\n"
        f"    if k ≥ {int(scan_cap)} then exit repeat\n"
        "  end repeat\n"
        "  return out\n"
        "end tell"
    )


async def _most_recent_note(scan_cap: int = 500) -> Match | None:
    """The most recently MODIFIED note (what the user means by 'la última nota').

    Modification date, not creation date — "last touched" is what he
    experiences in the Notes UI."""
    raw = await macos.osascript(_enumerate_light_script(scan_cap), timeout_s=_NOTES_TIMEOUT_S)
    matches = parse_matches(raw)
    if not matches:
        return None
    return max(matches, key=lambda m: m.when)


@tool(returns_untrusted_content=True)
async def resolve_recent_note() -> ToolResult:
    """Devuelve el título y un vistazo de la nota MÁS RECIENTE (última modificada).

    Úsalo para confirmar antes de actuar cuando the user diga 'la última nota' /
    'esa nota que acabo de crear' y no estés seguro de cuál es."""
    try:
        m = await _most_recent_note()
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer las notas: {exc}", False)
    if m is None:
        return ToolResult(True, {"title": None}, "No tienes notas todavía.", False)
    preview = ""
    try:
        body = await _read_body(m.id)
        preview = " ".join(body.split())[:120]
    except macos.AppleScriptError:
        pass  # the title alone is still useful
    return ToolResult(
        True,
        {"title": m.title, "modified": m.when, "preview": preview},
        f"Tu última nota es '{m.title}'.",
        False,
    )


@tool(returns_untrusted_content=True)
async def list_notes(query: str = "", limit: int = 10) -> ToolResult:
    """Lista tus notas de Apple Notes (título, fecha de modificación y un fragmento).

    `query` filtra por título (contiene)."""
    if query:
        f = macos.esc_applescript(query)
        clause = f' whose name contains "{f}"'
    else:
        clause = ""
    try:
        matches = await _enumerate(clause, limit)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer las notas: {exc}", False)
    if not matches:
        return ToolResult(True, {"notes": []}, "No encontré notas.", False)
    notes = [{"title": m.title, "modification_date": m.when, "preview": m.preview} for m in matches]
    spoken = "; ".join(m.title for m in matches)
    return ToolResult(True, {"notes": notes}, f"Tus notas: {spoken}.", False)


@tool(returns_untrusted_content=True)
async def search_notes(query: str, limit: int = 10) -> ToolResult:
    """Busca notas cuyo título contiene `query`."""
    f = macos.esc_applescript(query)
    try:
        matches = await _enumerate(f' whose name contains "{f}"', limit)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude buscar notas: {exc}", False)
    notes = [{"title": m.title, "modification_date": m.when, "preview": m.preview} for m in matches]
    spoken = "; ".join(m.title for m in matches) or "nada"
    return ToolResult(True, {"notes": notes}, f"Encontré: {spoken}.", False)


async def _find_normalized_duplicate(title: str) -> Match | None:
    """A note whose title equals `title` after punctuation/diacritics
    normalization (19.6-B23). Cheap scan — titles only, no bodies."""
    raw = await macos.osascript(_enumerate_light_script(200), timeout_s=_NOTES_TIMEOUT_S)
    want = normalize_title(title)
    for m in parse_matches(raw):
        if normalize_title(m.title) == want:
            return m
    return None


@tool()
async def create_note(
    title: str, body: str, folder: str = "", confirmed: bool = False
) -> ToolResult:
    """Crea una nota nueva en Apple Notes con `title` y `body`.

    Si ya existe una nota cuyo título solo difiere en puntuación/acentos
    ('Limitación: terminal Cursor' vs 'Limitación terminal Cursor'), pregunta
    primero: con el sí de the user para crear de todos modos, re-llama con
    confirmed=true; si prefiere agregar a la existente, usa append_to_note.
    `folder` es opcional; si se omite, usa la carpeta por defecto.
    """
    if not confirmed:
        try:
            near = await _find_normalized_duplicate(title)
        except macos.AppleScriptError:
            near = None  # the guard is best-effort; creation must not break
        if near is not None and near.title != title:
            return ToolResult(
                True,
                {"existing": near.title, "proposed": title},
                f"Ya tengo una nota llamada '{near.title}'. ¿Quieres agregar a "
                f"esa o crear una nueva con el título '{title}'?",
                requires_confirmation=True,
            )

    # 30: platform-specific note creation lives in core.platform.notes (Apple Notes
    # on mac; OneNote on Windows in 30.x). The osascript moved to _mac/notes.py — same
    # behavior on Mac, but on a platform without an impl the cross-platform error
    # becomes a friendly line instead of a stack trace.
    from core.platform import UnsupportedOnPlatform
    from core.platform import notes as platform_notes

    try:
        await platform_notes.get().create(title, body, folder)
    except UnsupportedOnPlatform as exc:
        return ToolResult(False, None, f"En este sistema no tengo {exc.capability} todavía.", False)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude crear la nota: {exc}", False)
    # 28: undo a creation by deleting it.
    reverse = episodic.blueprint_inverse("delete_note", {"title": title})
    return ToolResult(
        True, {"title": title, "_reverse_blueprint": reverse}, f"Listo, creé la nota '{title}'.", False
    )


def _lang() -> str:
    return dictionary.user_profile().get("preferred_lang", "es") or "es"


async def _append_to_match(match: Match, text: str) -> ToolResult:
    """Append ``text`` to one specific note (addressed by id, HTML-wrapped)."""
    x = macos.esc_applescript(text)
    nid = macos.esc_applescript(match.id)
    script = (
        f'tell application "Notes" to set body of (note id "{nid}") to '
        f'(body of (note id "{nid}")) & "<div>{x}</div>"'
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_NOTES_TIMEOUT_S, on_error="No pude agregar a la nota"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(True, {"title": match.title}, f"Agregado a '{match.title}'.", False)


async def _create_with_text(new_title: str, text: str, *, existed: bool) -> ToolResult:
    """Create a note titled ``new_title`` with ``text`` as the body (reusing
    create_note's HTML-body fix), with an append-flavored message. the user
    already confirmed the creation upstream — skip the near-dup guard."""
    res = await create_note(new_title, text, confirmed=True)
    if not res.success:
        return res
    msg = (
        f"No existía '{new_title}', la creé y agregué tu texto."
        if not existed
        else f"Creé '{new_title}' con tu texto."
    )
    return ToolResult(True, {"title": new_title, "created": True}, msg, False)


@tool()
async def append_to_note(
    title: str,
    text: str,
    index: int | None = None,
    suffix: str = "",
    create_if_missing: bool = False,
    recent: bool = False,
    confirmed: bool = False,
) -> ToolResult:
    """Agrega `text` a la nota `title`, con búsqueda flexible (Bug 19.5-B3).

    `recent=true` (19.6-B21): ignora `title` y agrega a la nota MÁS RECIENTE
    ('la última nota'). 1 coincidencia (exacta / empieza-con / contiene) →
    agrega directo. Varias → pregunta por el sufijo distintivo. Ninguna →
    ofrece crearla. Nunca agrega a la nota equivocada en silencio."""
    if recent:
        try:
            m = await _most_recent_note()
        except macos.AppleScriptError as exc:
            return ToolResult(False, None, f"No pude leer las notas: {exc}", False)
        if m is None:
            return ToolResult(False, None, "No tienes notas todavía; ¿creo una?", False)
        return await _append_to_match(m, text)

    t = macos.esc_applescript(title)
    try:
        matches, _strategy = await find_by_title(_enumerate, t, limit=25)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer las notas: {exc}", False)

    # An explicit suffix narrows a previously-ambiguous set ("el miércoles").
    if suffix and len(matches) > 1:
        s = suffix.strip().lower()
        survivors = [m for m in matches if m.title.lower().endswith(s)]
        if len(survivors) == 1:
            return await _append_to_match(survivors[0], text)
        if not survivors:
            prefix = word_common_prefix([m.title for m in matches])
            new_title = f"{prefix} {suffix}".strip()
            if create_if_missing and confirmed:
                return await _create_with_text(new_title, text, existed=False)
            return ToolResult(
                True,
                {"create_title": new_title},
                f"No tengo '{new_title}'. ¿La creo nueva?",
                requires_confirmation=True,
            )
        matches = survivors  # still >1 → fall through to a fresh suffix prompt

    # Explicit numeric pick (compat fallback).
    if index is not None and 1 <= index <= len(matches):
        return await _append_to_match(matches[index - 1], text)

    # 0 matches → offer to create (never invent silently).
    if not matches:
        if create_if_missing and confirmed:
            return await _create_with_text(title, text, existed=False)
        return ToolResult(
            True,
            {"title": title},
            f"No encontré una nota llamada '{title}'. ¿La creo nueva?",
            requires_confirmation=True,
        )

    # Exactly one → append (the user already said "agrega"; no extra confirm).
    if len(matches) == 1:
        return await _append_to_match(matches[0], text)

    # >1 → ask by distinguishing suffix.
    prefix = word_common_prefix([m.title for m in matches])
    return ToolResult(
        True,
        {"matches": [asdict(m) for m in matches], "prefix": prefix},
        suffix_prompt(matches, prefix, lang=_lang()),
        requires_confirmation=True,
    )


async def _read_body(note_id: str) -> str:
    """Return the plaintext body of one note, addressed by id."""
    nid = macos.esc_applescript(note_id)
    script = f'tell application "Notes" to return plaintext of note id "{nid}"'
    return await macos.osascript(script, timeout_s=_NOTES_TIMEOUT_S)


@tool(returns_untrusted_content=True)
async def read_note(title: str, index: int | None = None, recent: bool = False) -> ToolResult:
    """Lee el contenido de la nota cuyo título es `title`.

    `recent=true` ignora `title` y lee la nota MÁS RECIENTE ('la última
    nota', 19.6-B21). Si hay varias con el mismo nombre, pide cuál."""
    if recent:
        try:
            chosen = await _most_recent_note()
        except macos.AppleScriptError as exc:
            return ToolResult(False, None, f"No pude leer las notas: {exc}", False)
        if chosen is None:
            return ToolResult(True, {"notes": []}, "No tienes notas todavía.", False)
    else:
        t = macos.esc_applescript(title)
        try:
            matches = await _enumerate_by_title(t, limit=25)
        except macos.AppleScriptError as exc:
            return ToolResult(False, None, f"No pude leer las notas: {exc}", False)
        chosen, response = disambiguate(matches, index, noun="nota", title=title)
        if response is not None:
            return response
    assert chosen is not None
    try:
        body = await _read_body(chosen.id)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer la nota: {exc}", False)
    return ToolResult(
        True, {"title": chosen.title, "body": body}, body or "La nota está vacía.", False
    )


@tool(destructive=True)
async def merge_notes(
    source_title: str,
    target_title: str,
    separator: str = "\n\n---\n\n",
    source_index: int | None = None,
    target_index: int | None = None,
    recent: bool = False,
    confirmed: bool = False,
) -> ToolResult:
    """Fusiona dos notas: agrega el contenido de `source_title` al final de
    `target_title` y borra la nota origen. Pide confirmación.

    `recent=true` (19.6-B21): la nota ORIGEN es la más reciente ('fusiona la
    última nota con X'). Si algún título coincide con varias, pide cuál."""
    tt = macos.esc_applescript(target_title)
    try:
        if recent:
            src = await _most_recent_note()
            if src is None:
                return ToolResult(True, {"matches": 0}, "No tienes notas todavía.", False)
        tgt_matches = await _enumerate_by_title(tt, limit=25)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude leer las notas: {exc}", False)

    if not recent:
        st = macos.esc_applescript(source_title)
        try:
            src_matches = await _enumerate_by_title(st, limit=25)
        except macos.AppleScriptError as exc:
            return ToolResult(False, None, f"No pude leer las notas: {exc}", False)
        src, resp = disambiguate(src_matches, source_index, noun="nota origen", title=source_title)
        if resp is not None:
            return resp
    tgt, resp = disambiguate(tgt_matches, target_index, noun="nota destino", title=target_title)
    if resp is not None:
        return resp
    assert src is not None and tgt is not None
    if src.id == tgt.id:
        return ToolResult(False, None, "Son la misma nota; no hay nada que fusionar.", False)

    if not confirmed:
        return ToolResult(
            True,
            {"source": src.title, "target": tgt.title},
            f"¿Fusiono '{src.title}' dentro de '{tgt.title}' y borro la origen?",
            True,
        )

    sep_html = "<div><br></div>" + macos.esc_applescript(separator).replace("\n", "<br>")
    sid = macos.esc_applescript(src.id)
    tid = macos.esc_applescript(tgt.id)
    script = (
        'tell application "Notes"\n'
        f'  set srcNote to note id "{sid}"\n'
        f'  set tgtNote to note id "{tid}"\n'
        f'  set body of tgtNote to (body of tgtNote) & "{sep_html}" & (body of srcNote)\n'
        "  delete srcNote\n"
        "end tell"
    )
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_NOTES_TIMEOUT_S, on_error="No pude fusionar las notas"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(
        True,
        {"source": src.title, "target": tgt.title},
        f"Listo, fusioné '{src.title}' en '{tgt.title}'.",
        False,
    )


@tool(destructive=True)
async def delete_note(
    title: str, index: int | None = None, recent: bool = False, confirmed: bool = False
) -> ToolResult:
    """Borra la nota de Apple Notes cuyo título es `title`. Pide confirmación.

    `recent=true` ignora `title` y apunta a la nota MÁS RECIENTE (19.6-B21).
    Si hay varias con el mismo nombre, las enumera y pide cuál (por número) —
    nunca borra todas (Bug 19.2-B2)."""
    if recent:
        try:
            chosen = await _most_recent_note()
        except macos.AppleScriptError as exc:
            return ToolResult(False, None, f"No pude leer las notas: {exc}", False)
        if chosen is None:
            return ToolResult(True, {"matches": 0}, "No tienes notas todavía.", False)
    else:
        t = macos.esc_applescript(title)
        try:
            matches = await _enumerate_by_title(t, limit=25)
        except macos.AppleScriptError as exc:
            return ToolResult(False, None, f"No pude leer las notas: {exc}", False)

        chosen, response = disambiguate(matches, index, noun="nota", title=title)
        if response is not None:
            return response
    assert chosen is not None

    if not confirmed:
        when = f" (modificada {chosen.when})" if chosen.when else ""
        return ToolResult(
            True,
            {"title": chosen.title, "id": chosen.id},
            f"¿Borro la nota '{chosen.title}'{when}?",
            True,
        )

    nid = macos.esc_applescript(chosen.id)
    # 28: capture the body BEFORE deleting so undo can recreate the note.
    try:
        saved_body = await _read_body(chosen.id)
    except Exception:
        saved_body = ""
    script = f'tell application "Notes" to delete (note id "{nid}")'
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_NOTES_TIMEOUT_S, on_error="No pude borrar la nota"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    if saved_body.strip():
        reverse = episodic.blueprint_inverse(
            "create_note", {"title": chosen.title, "body": saved_body}
        )
    else:
        reverse = episodic.blueprint_manual(
            f"No guardé el contenido de '{chosen.title}'. Restáurala desde "
            "'Eliminados recientemente' en Apple Notes."
        )
    return ToolResult(
        True,
        {"deleted": 1, "title": chosen.title, "_reverse_blueprint": reverse},
        f"Listo, borré la nota '{chosen.title}'.",
        False,
    )
