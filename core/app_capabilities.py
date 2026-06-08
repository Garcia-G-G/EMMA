"""App-capabilities registry loader.

Loads config/app_capabilities.toml into a module cache. Same hot-reload +
injection-safe append semantics as core/dictionary.py. The registry is the
growth surface for app control — apps are TOML blocks, not code.
"""

from __future__ import annotations

import re
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger("emma.app_capabilities")

_CAPS_PATH = Path(__file__).resolve().parent.parent / "config" / "app_capabilities.toml"
_LOCK = threading.RLock()


@dataclass
class Capabilities:
    app: str
    url_scheme: str = ""
    applescript_dict: str = ""
    cli: str = ""
    category: str = ""
    notes: str = ""
    # kind → URL template with {placeholders} (19.6-B17). Placeholder names
    # match the keys stored in dictionary [user_apps.<app>] / [connections.*],
    # e.g. "tableplus://connect/{name}", "slack://channel?team={workspace}&id={channel}".
    resource_url: dict[str, str] = field(default_factory=dict)
    # kind → https:// composer URL used when the app's own scheme is unreliable
    # or absent (e.g. X has no macOS app since 2024 → web intent; Prompt 26).
    web_fallback: dict[str, str] = field(default_factory=dict)


_caps: dict[str, Capabilities] = {}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _str_map(raw: object) -> dict[str, str]:
    """A TOML sub-table → {str: str}, dropping non-string values."""
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(u) for k, u in raw.items() if isinstance(u, str)}


def _parse() -> None:
    if not _CAPS_PATH.exists():
        log.warning("app_capabilities_missing", path=str(_CAPS_PATH))
        return
    try:
        with _CAPS_PATH.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        log.error("app_capabilities_parse_failed", error=str(exc))
        return
    with _LOCK:
        _caps.clear()
        for app, v in data.items():
            if not isinstance(v, dict):
                continue
            _caps[app] = Capabilities(
                app=app,
                url_scheme=v.get("url_scheme", ""),
                applescript_dict=v.get("applescript_dict", ""),
                cli=v.get("cli", ""),
                category=v.get("category", ""),
                notes=v.get("notes", ""),
                resource_url=_str_map(v.get("resource_url")),
                web_fallback=_str_map(v.get("web_fallback")),
            )


def reload() -> int:
    _parse()
    log.info("app_capabilities_loaded", count=len(_caps))
    return len(_caps)


_parse()  # warm cache on import


def caps_for(app: str) -> Capabilities | None:
    """Case-insensitive lookup. 'Slack', 'slack', 'GitHub Desktop' all resolve."""
    return _caps.get(_slug(app))


def apps_with(field: str) -> list[str]:
    """App slugs whose `field` is non-empty (e.g. apps_with('url_scheme'))."""
    return sorted(k for k, c in _caps.items() if getattr(c, field, ""))


def apps_in_category(category: str) -> list[str]:
    cat = category.strip().lower()
    return sorted(k for k, c in _caps.items() if c.category.lower() == cat)


def all_apps() -> list[Capabilities]:
    return list(_caps.values())


# ---- Append helper (mirrors core.vocabulary / core.dictionary) ------------


def _toml_escape(s: str) -> str:
    """Strip control chars (TOML-injection guard) then escape backslash + quote."""
    s = "".join(ch for ch in s if ord(ch) >= 0x20)
    return s.replace("\\", "\\\\").replace('"', '\\"')


def append_app(name: str, **fields: str) -> str:
    """Append a capability block and reload. Returns the slug written."""
    slug = _slug(name) or "app"
    lines = [f"\n[{slug}]"]
    for k, v in fields.items():
        if v:
            lines.append(f'{k} = "{_toml_escape(str(v))}"')
    with _LOCK:
        with _CAPS_PATH.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        reload()
    return slug
