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

import structlog

from actions import macos
from tools.base import ToolResult, tool
from tools.disambiguation import (
    FIELD_SEP,
    ISO_DATE_HANDLER,
    Match,
    disambiguate,
    parse_matches,
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


# Recently-Deleted folder names to skip during enumeration (Garcia's locales:
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
    """Find notes by exact title, falling back to a ``contains`` match.

    Notes created before the HTML-body fix have their title flattened together
    with their body (e.g. name = "Errores de Emma Bitácora…"), so an exact
    ``name is`` lookup misses them. The ``contains`` fallback lets read/delete
    still resolve those; disambiguation handles any extra matches it surfaces.
    """
    matches = await _enumerate(f' whose name is "{title_esc}"', limit)
    if not matches:
        matches = await _enumerate(f' whose name contains "{title_esc}"', limit)
    return matches


@tool()
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


@tool()
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


@tool()
async def create_note(title: str, body: str, folder: str = "") -> ToolResult:
    """Crea una nota nueva en Apple Notes con `title` y `body`.

    `folder` es opcional; si se omite, usa la carpeta por defecto.
    """
    t = macos.esc_applescript(title)
    b = macos.esc_applescript(body)
    # HTML body so Notes keeps the title (first line) clean and SEPARATE from the
    # body. A plain "title\nbody" flattens into one title, which then breaks
    # exact-title lookup in read_note/delete_note (19.4-followup fix).
    html_body = f"<div>{t}</div><div>{b}</div>" if body else f"<div>{t}</div>"
    note_props = f'{{body:"{html_body}"}}'
    if folder:
        f = macos.esc_applescript(folder)
        # Create the folder on demand instead of failing with -1728.
        make = (
            f'if not (exists folder "{f}") then make new folder with properties {{name:"{f}"}}\n'
            f'tell folder "{f}" to make new note with properties {note_props}'
        )
    else:
        make = f"make new note with properties {note_props}"
    script = f'tell application "Notes"\n{make}\nend tell'
    try:
        await macos.osascript(script, timeout_s=_NOTES_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude crear la nota: {exc}", False)
    return ToolResult(True, {"title": title}, f"Listo, creé la nota '{title}'.", False)


@tool()
async def append_to_note(title: str, text: str) -> ToolResult:
    """Agrega `text` al final de la nota cuyo título es `title`."""
    t = macos.esc_applescript(title)
    x = macos.esc_applescript(text)
    script = (
        'tell application "Notes"\n'
        f'  set matches to (notes whose name is "{t}")\n'
        '  if (count of matches) is 0 then error "no encontré esa nota"\n'
        "  set theNote to item 1 of matches\n"
        f'  set body of theNote to (body of theNote) & "<div>{x}</div>"\n'
        "end tell"
    )
    try:
        await macos.osascript(script, timeout_s=_NOTES_TIMEOUT_S)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude agregar a la nota: {exc}", False)
    return ToolResult(True, {"title": title}, f"Agregado a '{title}'.", False)


async def _read_body(note_id: str) -> str:
    """Return the plaintext body of one note, addressed by id."""
    nid = macos.esc_applescript(note_id)
    script = f'tell application "Notes" to return plaintext of note id "{nid}"'
    return await macos.osascript(script, timeout_s=_NOTES_TIMEOUT_S)


@tool()
async def read_note(title: str, index: int | None = None) -> ToolResult:
    """Lee el contenido de la nota cuyo título es `title`.

    Si hay varias con el mismo nombre, pide cuál (por número)."""
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
    confirmed: bool = False,
) -> ToolResult:
    """Fusiona dos notas: agrega el contenido de `source_title` al final de
    `target_title` y borra la nota origen. Pide confirmación.

    Si algún título coincide con varias notas, pide cuál (por número)."""
    st = macos.esc_applescript(source_title)
    tt = macos.esc_applescript(target_title)
    try:
        src_matches = await _enumerate_by_title(st, limit=25)
        tgt_matches = await _enumerate_by_title(tt, limit=25)
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
async def delete_note(title: str, index: int | None = None, confirmed: bool = False) -> ToolResult:
    """Borra la nota de Apple Notes cuyo título es `title`. Pide confirmación.

    Si hay varias con el mismo nombre, las enumera y pide cuál (por número) —
    nunca borra todas (Bug 19.2-B2)."""
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
    script = f'tell application "Notes" to delete (note id "{nid}")'
    ok, out = await macos.osascript_or_friendly(
        script, timeout_s=_NOTES_TIMEOUT_S, on_error="No pude borrar la nota"
    )
    if not ok:
        return ToolResult(False, None, out, False)
    return ToolResult(
        True,
        {"deleted": 1, "title": chosen.title},
        f"Listo, borré la nota '{chosen.title}'.",
        False,
    )
