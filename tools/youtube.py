"""YouTube Data API v3 search and open-in-browser helpers."""
from __future__ import annotations

import httpx
import structlog

from actions import macos
from config.settings import settings
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.youtube")

_API = "https://www.googleapis.com/youtube/v3"


def _missing_key() -> ToolResult:
    return ToolResult(
        False,
        None,
        "No tengo una llave de YouTube configurada todavía.",
        False,
    )


def _api_get(path: str, params: dict[str, str]) -> dict | None:
    if not settings.YOUTUBE_API_KEY:
        return None
    full = {"key": settings.YOUTUBE_API_KEY, **params}
    try:
        r = httpx.get(f"{_API}/{path}", params=full, timeout=settings.API_TIMEOUT_S)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("youtube_http_failed", error=str(exc))
        return None
    return r.json()


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

    top = channels[0]
    others = channels[1:3]
    top_title = top["snippet"]["title"]
    if others and any(
        c["snippet"]["title"].lower() != top_title.lower() for c in others
    ):
        names = ", ".join(c["snippet"]["title"] for c in [top, *others])
        return ToolResult(
            True,
            {"candidates": [c["snippet"]["title"] for c in [top, *others]]},
            f"Encontré varios canales: {names}. ¿Cuál?",
            requires_confirmation=True,
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
