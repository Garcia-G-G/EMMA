"""YouTube Data API v3 search and open-in-browser helpers."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from actions import macos
from config.settings import settings
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.youtube")

_API = "https://www.googleapis.com/youtube/v3"


def _normalize_channel_name(s: str) -> str:
    """Lowercase, strip whitespace, drop common leading articles, collapse spaces.

    Used to compare the user's spoken creator name against YouTube's
    channel titles for an exact-match short-circuit in
    :func:`latest_video_from_creator`.
    """
    s = s.strip().lower()
    for prefix in ("el ", "la ", "the "):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    return " ".join(s.split())


def _missing_key() -> ToolResult:
    return ToolResult(
        False,
        None,
        "No tengo una llave de YouTube configurada todavía.",
        False,
    )


def _api_get(path: str, params: dict[str, str]) -> dict[str, Any] | None:
    if not settings.YOUTUBE_API_KEY:
        return None
    full = {"key": settings.YOUTUBE_API_KEY, **params}
    try:
        r = httpx.get(f"{_API}/{path}", params=full, timeout=settings.API_TIMEOUT_S)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("youtube_http_failed", error=str(exc))
        return None
    data: dict[str, Any] = r.json()
    return data


@tool()
def latest_video_from_creator(creator_name: str) -> ToolResult:
    """Open the most recent video from a YouTube creator/channel.

    Searches by channel name; if multiple channels match closely, asks the
    user to pick one.
    """
    if not settings.YOUTUBE_API_KEY:
        return _missing_key()

    ch = _api_get(
        "search",
        {
            "q": creator_name,
            "type": "channel",
            "part": "snippet",
            "maxResults": "5",
        },
    )
    if ch is None:
        return ToolResult(False, None, "No pude consultar YouTube.", False)
    channels = ch.get("items", [])
    if not channels:
        return ToolResult(False, None, f"No encontré el canal '{creator_name}'.", False)

    # Short-circuit on an exact match: if the user's query normalizes to
    # the same form as one of the candidates, take that one directly. Avoids
    # the loop where the LLM keeps re-asking with the same exact name.
    query_norm = _normalize_channel_name(creator_name)
    exact = next(
        (c for c in channels if _normalize_channel_name(c["snippet"]["title"]) == query_norm),
        None,
    )
    if exact is not None:
        top = exact
        top_title = top["snippet"]["title"]
        log.info("youtube_exact_match", channel=top_title, query=creator_name)
    else:
        top = channels[0]
        others = channels[1:3]
        top_title = top["snippet"]["title"]
        if others and any(c["snippet"]["title"].lower() != top_title.lower() for c in others):
            names = ", ".join(c["snippet"]["title"] for c in [top, *others])
            # Disambiguation is NOT a yes/no flow. Return success so the LLM
            # speaks the options and the user picks one in their next turn;
            # the LLM then re-calls this tool with a more specific name.
            return ToolResult(
                success=True,
                data={"candidates": [c["snippet"]["title"] for c in [top, *others]]},
                user_message=(
                    f"Hay varios canales que coinciden: {names}. "
                    "¿Cuál quieres? Dímelo con más detalle."
                ),
                requires_confirmation=False,
            )

    channel_id = top["snippet"]["channelId"]
    vids = _api_get(
        "search",
        {
            "channelId": channel_id,
            "type": "video",
            "order": "date",
            "part": "snippet",
            "maxResults": "1",
        },
    )
    if vids is None or not vids.get("items"):
        return ToolResult(False, None, f"El canal {top_title} no tiene videos recientes.", False)

    video = vids["items"][0]
    video_id = video["id"]["videoId"]
    title = video["snippet"]["title"]
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        macos.open_url(url)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude abrir el navegador: {exc}", False)
    return ToolResult(
        True,
        {"video_id": video_id, "url": url, "channel": top_title, "title": title},
        f"Abriendo el video más reciente de {top_title}: {title}.",
        False,
    )


@tool()
def search_and_open(query: str) -> ToolResult:
    """Search YouTube and open the top video result in the browser."""
    if not settings.YOUTUBE_API_KEY:
        return _missing_key()
    res = _api_get(
        "search",
        {"q": query, "type": "video", "part": "snippet", "maxResults": "1"},
    )
    if res is None or not res.get("items"):
        return ToolResult(False, None, f"No encontré nada para '{query}'.", False)
    video = res["items"][0]
    video_id = video["id"]["videoId"]
    title = video["snippet"]["title"]
    channel = video["snippet"]["channelTitle"]
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        macos.open_url(url)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"No pude abrir el navegador: {exc}", False)
    return ToolResult(
        True,
        {"video_id": video_id, "url": url, "title": title, "channel": channel},
        f"Abriendo {title} de {channel}.",
        False,
    )
