"""Knowledge dictionary loader.

Five views derived from ``config/dictionary.toml``. Lazy + hot-reloadable via
``reload()``. Module-level cache; safe to import many times.

Read-only at runtime EXCEPT through the ``append_*`` helpers, which append
safely-escaped blocks to the TOML file (mirrors ``core.vocabulary.append_entry``,
including the control-character stripping that closes the TOML-injection hole).
"""

from __future__ import annotations

import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger("emma.dictionary")

_DICT_PATH = Path(__file__).resolve().parent.parent / "config" / "dictionary.toml"
_LOCK = threading.RLock()


@dataclass
class Page:
    key: str
    url: str
    title: str
    open_in: str = ""


@dataclass
class Contact:
    key: str
    name: str
    email: str
    relation: str
    aliases: list[str]


@dataclass
class Term:
    key: str
    expansion: str
    context: str


@dataclass
class Fact:
    key: str
    text: str
    kind: str
    confidence: float


_pages: dict[str, Page] = {}
_contacts: dict[str, Contact] = {}
_terms: dict[str, Term] = {}
_apps: dict[str, str] = {}
_facts: dict[str, Fact] = {}
_user_apps: dict[str, dict[str, Any]] = {}
_user: dict[str, str] = {}
_connections: dict[str, dict[str, Any]] = {}

# The identity fields Emma recognises. Single source of truth for "yo/mi/mis".
_USER_FIELDS = (
    "display_name",
    "full_name",
    "github_username",
    "linkedin",
    "website",
    "preferred_lang",
)


def _parse() -> None:
    if not _DICT_PATH.exists():
        log.warning("dictionary_missing", path=str(_DICT_PATH))
        return
    try:
        with _DICT_PATH.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        log.error("dictionary_parse_failed", error=str(exc))
        return
    with _LOCK:
        _pages.clear()
        _contacts.clear()
        _terms.clear()
        _apps.clear()
        _facts.clear()
        _user_apps.clear()
        _user.clear()
        _connections.clear()
        user_tbl = data.get("user") or {}
        if isinstance(user_tbl, dict):
            for fld in _USER_FIELDS:
                val = user_tbl.get(fld, "")
                _user[fld] = str(val) if val is not None else ""
        for k, v in (data.get("pages") or {}).items():
            _pages[k] = Page(
                key=k, url=v.get("url", ""), title=v.get("title", ""), open_in=v.get("open_in", "")
            )
        for k, v in (data.get("contacts") or {}).items():
            _contacts[k] = Contact(
                key=k,
                name=v.get("name", ""),
                email=v.get("email", ""),
                relation=v.get("relation", ""),
                aliases=list(v.get("aliases", [])),
            )
        for k, v in (data.get("terms") or {}).items():
            _terms[k] = Term(key=k, expansion=v.get("expansion", ""), context=v.get("context", ""))
        for k, v in (data.get("apps") or {}).items():
            _apps[k] = v.get("default", "")
        for k, v in (data.get("user_apps") or {}).items():
            if isinstance(v, dict):
                _user_apps[k.lower()] = dict(v)
        for k, v in (data.get("connections") or {}).items():
            if isinstance(v, dict):
                entry = dict(v)
                entry.setdefault("name", k)
                entry.setdefault("kind", "connection")
                _connections[k] = entry
        for k, v in (data.get("facts") or {}).items():
            _facts[k] = Fact(
                key=k,
                text=v.get("text", ""),
                kind=v.get("kind", "general"),
                confidence=float(v.get("confidence", 0.8)),
            )


def reload() -> int:
    _parse()
    user_count = 1 if any(v.strip() for v in _user.values()) else 0
    total = len(_pages) + len(_contacts) + len(_terms) + len(_apps) + len(_facts) + user_count
    log.info(
        "dictionary_loaded",
        pages=len(_pages),
        contacts=len(_contacts),
        terms=len(_terms),
        apps=len(_apps),
        facts=len(_facts),
        user=user_count,
    )
    return total


_parse()  # warm cache on import


# ---- Public lookup helpers ------------------------------------------------


def pages() -> dict[str, Page]:
    return dict(_pages)


def contacts() -> dict[str, Contact]:
    return dict(_contacts)


def terms() -> dict[str, Term]:
    return dict(_terms)


def app_for(category: str) -> str:
    return _apps.get(category, "")


def apps_preferences() -> dict[str, str]:
    """Category → preferred app display name (Cursor, Brave Browser, …)."""
    return dict(_apps)


def user_profile() -> dict[str, str]:
    """Garcia's identity fields (copy). Empty strings for anything unset.

    The single source of truth for "yo/mi/mis"; secrets never live here.
    """
    return {fld: _user.get(fld, "") for fld in _USER_FIELDS}


def set_user_field(field: str, value: str) -> bool:
    """Set one identity field in the ``[user]`` block (last-write-wins).

    TOML forbids redeclaring a table/key, so this rewrites the whole ``[user]``
    block in place rather than appending. Returns False for an unknown field.
    """
    field = field.strip().lower()
    if field not in _USER_FIELDS:
        return False
    with _LOCK:
        current = {fld: _user.get(fld, "") for fld in _USER_FIELDS}
        current[field] = value.strip()
        _rewrite_user_block(current)
        reload()
    return True


def _rewrite_user_block(values: dict[str, str]) -> None:
    """Replace the ``[user]`` table in the TOML file with ``values`` (or append
    it if absent). Only the ``[user]`` block is touched; everything else is kept
    byte-for-byte. Values flow through :func:`_toml_escape`."""
    block_lines = ["[user]"]
    for fld in _USER_FIELDS:
        block_lines.append(f'{fld} = "{_toml_escape(values.get(fld, ""))}"')
    block = "\n".join(block_lines)

    text = _DICT_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        if lines[i].strip() == "[user]":
            # Replace the old block: its header + its ``key = "…"`` lines. Stop at
            # the first blank line, comment, or next section header so we never
            # eat the following section's comments.
            out.append(block)
            replaced = True
            i += 1
            while i < len(lines):
                s = lines[i].lstrip()
                if s == "" or s.startswith("#") or s.startswith("["):
                    break
                i += 1
            continue
        out.append(lines[i])
        i += 1
    if not replaced:
        out.append("")
        out.append(block)
    _DICT_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def user_app(name: str) -> dict[str, Any]:
    """Per-user config for an app (workspace/team/vault IDs), or {}."""
    return dict(_user_apps.get(name.strip().lower(), {}))


def connections() -> dict[str, dict[str, Any]]:
    """Per-user in-app resources (TablePlus connections, Slack channels…).

    Each entry carries at least ``app``, ``kind`` and ``name`` (19.6-B17).
    """
    return {k: dict(v) for k, v in _connections.items()}


def find_connection(query: str) -> dict[str, Any] | None:
    """Case-insensitive lookup by section key or ``name`` field."""
    q = query.strip().lower()
    if not q:
        return None
    for k, v in _connections.items():
        if q == k.lower() or q == str(v.get("name", "")).lower():
            return dict(v)
    return None


def facts() -> list[Fact]:
    return list(_facts.values())


def find_page(query: str) -> Page | None:
    q = query.strip().lower()
    for p in _pages.values():
        if q == p.key.lower() or q in p.title.lower():
            return p
    return None


def find_contact(query: str) -> Contact | None:
    q = query.strip().lower()
    for c in _contacts.values():
        if q == c.key.lower() or q == c.name.lower() or q == c.relation.lower():
            return c
        if q in [a.lower() for a in c.aliases]:
            return c
    return None


def expand_term(term: str) -> Term | None:
    return _terms.get(term.upper()) or _terms.get(term)


# ---- Append helpers (mirror vocabulary.append_entry pattern) -------------


def _toml_escape(s: str) -> str:
    """Escape for a TOML basic string. Strips control chars first (so a crafted
    value can't break the single-line string or inject a [section])."""
    s = "".join(ch for ch in s if ord(ch) >= 0x20)
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _slug(s: str) -> str:
    import re

    out = re.sub(r"[^A-Za-z0-9_]", "_", s.strip())
    return out or "entry"


def _append_block(section: str, slug: str, fields: dict[str, Any]) -> None:
    slug = _slug(slug)
    lines = [f"\n[{section}.{slug}]"]
    for k, v in fields.items():
        if v is None or v == "":
            continue
        if isinstance(v, list):
            rendered = ", ".join(f'"{_toml_escape(str(x))}"' for x in v)
            lines.append(f"{k} = [{rendered}]")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            lines.append(f"{k} = {v}")
        else:
            lines.append(f'{k} = "{_toml_escape(str(v))}"')
    with _LOCK:
        with _DICT_PATH.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        reload()


def append_page(slug: str, url: str, title: str = "", open_in: str = "") -> None:
    _append_block("pages", slug, {"url": url, "title": title or slug, "open_in": open_in})


def append_contact(
    slug: str,
    name: str,
    email: str = "",
    relation: str = "",
    aliases: list[str] | None = None,
) -> None:
    _append_block(
        "contacts",
        slug,
        {"name": name, "email": email, "relation": relation, "aliases": aliases or []},
    )


def append_term(key: str, expansion: str, context: str = "") -> None:
    _append_block("terms", key, {"expansion": expansion, "context": context})


def append_fact(slug: str, text: str, kind: str = "general", confidence: float = 0.85) -> None:
    _append_block("facts", slug, {"text": text, "kind": kind, "confidence": confidence})


def append_connection(name: str, app: str, kind: str = "connection", **fields: str) -> str:
    """Append a ``[connections.<slug>]`` block. Keeps dashes in the slug —
    TablePlus connection names like ``learning-rots-local`` are valid TOML
    bare keys and must round-trip verbatim (19.6-B17)."""
    import re

    slug = re.sub(r"[^A-Za-z0-9_-]", "-", name.strip().lower()).strip("-") or "connection"
    lines = [f"\n[connections.{slug}]"]
    payload: dict[str, str] = {"app": app, "kind": kind, "name": name, **fields}
    for k, v in payload.items():
        if v:
            lines.append(f'{k} = "{_toml_escape(str(v))}"')
    with _LOCK:
        with _DICT_PATH.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        reload()
    return slug
