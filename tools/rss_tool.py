"""RSS/Atom headlines (Prompt 38-E). stdlib xml.etree; 5-min per-feed cache."""

from __future__ import annotations

import time
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import structlog

from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.rss")

_CACHE_TTL = 300.0
_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_FEEDS_TOML = Path(__file__).resolve().parent.parent / "config" / "dictionary.toml"


def _resolve_feed(feed: str) -> str | None:
    """A URL passes through; a name is looked up in dictionary.toml's [feeds]."""
    feed = (feed or "").strip()
    if feed.lower().startswith("http"):
        return feed
    try:
        with _FEEDS_TOML.open("rb") as fh:
            feeds = tomllib.load(fh).get("feeds", {})
    except Exception:
        return None
    want = feed.lower()
    for key, val in feeds.items():
        url = val if isinstance(val, str) else str(val.get("url", ""))
        names = [key.lower()] + ([str(val.get("name", "")).lower()] if isinstance(val, dict) else [])
        if want in names or any(want in n for n in names if n):
            return url or None
    return None


def _text(elem: ET.Element | None) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def _parse(xml: str) -> list[dict[str, str]]:
    """Items from RSS 2.0 (item/title/link text) or Atom (entry/title/link[@href])."""
    items: list[dict[str, str]] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return items
    for node in root.iter():
        tag = node.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = link = ""
        for child in node:
            ctag = child.tag.split("}")[-1]
            if ctag == "title":
                title = _text(child)
            elif ctag == "link":
                link = child.get("href") or _text(child)  # Atom uses @href, RSS uses text
        if title:
            items.append({"title": title, "link": link})
    return items


async def _fetch(url: str) -> list[dict[str, str]]:
    now = time.time()
    cached = _cache.get(url)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Emma/1.0 (+rss)"})
    items = _parse(resp.text)
    _cache[url] = (now, items)
    return items


@tool()
async def rss_latest(feed_url: str, n: int = 5) -> ToolResult:
    """Lee los últimos titulares de un feed RSS ("últimas de Hacker News").

    `feed_url` puede ser una URL o el nombre de un feed guardado en tu diccionario.
    """
    url = _resolve_feed(feed_url)
    if not url:
        return ToolResult(False, None, f"No conozco el feed «{feed_url}». Dame su URL.", False)
    try:
        items = await _fetch(url)
    except Exception as exc:
        log.warning("rss_fetch_failed", url=url, error=str(exc))
        return ToolResult(False, None, "No pude leer ese feed ahora mismo.", False)
    if not items:
        return ToolResult(True, {"items": []}, "Ese feed no tiene titulares ahora.", False)
    top = items[: max(1, min(int(n), 15))]
    titles = [it["title"] for it in top]
    spoken = "Lo último: " + "; ".join(titles[:5]) + "."
    return ToolResult(True, {"items": top}, spoken, False)
