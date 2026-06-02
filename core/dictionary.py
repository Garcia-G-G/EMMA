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
        for k, v in (data.get("facts") or {}).items():
            _facts[k] = Fact(
                key=k,
                text=v.get("text", ""),
                kind=v.get("kind", "general"),
                confidence=float(v.get("confidence", 0.8)),
            )


def reload() -> int:
    _parse()
    total = len(_pages) + len(_contacts) + len(_terms) + len(_apps) + len(_facts)
    log.info(
        "dictionary_loaded",
        pages=len(_pages),
        contacts=len(_contacts),
        terms=len(_terms),
        apps=len(_apps),
        facts=len(_facts),
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


def user_app(name: str) -> dict[str, Any]:
    """Per-user config for an app (workspace/team/vault IDs), or {}."""
    return dict(_user_apps.get(name.strip().lower(), {}))


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
