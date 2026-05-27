"""Music control: Spotify Web API when configured, Apple Music fallback.

Spotify path: spotipy drives an Auth-Code-with-PKCE flow on first run
(the prompt called it "device flow" - that's not what Spotify supports;
this is the closest analog). The browser opens once for consent and the
refresh token is cached at ``~/.emma/spotify_token.json``.

If `SPOTIFY_CLIENT_ID`/`SECRET` are missing, every Spotify call returns a
clean failure and the AppleScript-driven Apple Music path takes over.

# TODO(phase-06): hook into actions.environment.detect_preferred("music")
#   to surface an install prompt when the user prefers Spotify but it's
#   not installed. In practice detect_preferred("music") never returns
#   None because Apple Music ships with macOS, so the existing two-tier
#   fallback already covers all realistic configurations.
"""

from __future__ import annotations

from typing import Any

import structlog

from actions import macos
from config.settings import settings
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.music")

_SCOPES = "user-modify-playback-state user-read-playback-state user-read-currently-playing"
_spotify_client: Any = None
_spotify_unavailable = False


def _have_spotify_creds() -> bool:
    return bool(settings.SPOTIFY_CLIENT_ID and settings.SPOTIFY_CLIENT_SECRET)


def _spotify() -> Any:
    """Lazy-init the Spotify client. Returns None if unavailable."""
    global _spotify_client, _spotify_unavailable
    if _spotify_unavailable:
        return None
    if _spotify_client is not None:
        return _spotify_client
    if not _have_spotify_creds():
        _spotify_unavailable = True
        return None
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth

        cache_path = settings.EMMA_HOME / "spotify_token.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        auth = SpotifyOAuth(
            client_id=settings.SPOTIFY_CLIENT_ID,
            client_secret=settings.SPOTIFY_CLIENT_SECRET,
            redirect_uri=settings.SPOTIFY_REDIRECT_URI,
            scope=_SCOPES,
            cache_path=str(cache_path),
            open_browser=True,
        )
        _spotify_client = spotipy.Spotify(auth_manager=auth)
        return _spotify_client
    except Exception as exc:
        log.error("spotify_init_failed", error=str(exc))
        _spotify_unavailable = True
        return None


def _missing_creds_result() -> ToolResult:
    return ToolResult(
        False,
        None,
        "No tengo credenciales de Spotify configuradas todavía. Usaré Música si lo intentas de nuevo.",
        False,
    )


def _apple_music(script: str, ok_msg: str) -> ToolResult:
    try:
        macos.run_applescript(script)
    except macos.AppleScriptError as exc:
        return ToolResult(False, None, f"Música no respondió: {exc}", False)
    return ToolResult(True, None, ok_msg, False)


@tool()
def play_track(query: str) -> ToolResult:
    """Search for a track or artist and start playing the top result on Spotify."""
    sp = _spotify()
    if sp is None:
        return _apple_music(
            f'tell application "Music" to (play (first track of (search library 1 for "{query}")))',
            f"Reproduciendo {query} en Música.",
        )
    try:
        results = sp.search(q=query, type="track", limit=1)
        items = results.get("tracks", {}).get("items", [])
        if not items:
            return ToolResult(False, None, f"No encontré nada para '{query}'.", False)
        track = items[0]
        uri = track["uri"]
        try:
            sp.start_playback(uris=[uri])
        except Exception:
            return ToolResult(
                False,
                None,
                "Abre Spotify en algún dispositivo y vuelve a intentarlo.",
                False,
            )
        title = track["name"]
        artist = ", ".join(a["name"] for a in track.get("artists", []))
        return ToolResult(
            True,
            {"uri": uri, "title": title, "artist": artist},
            f"Reproduciendo {title} de {artist}.",
            False,
        )
    except Exception as exc:
        log.error("spotify_play_failed", error=str(exc))
        return ToolResult(False, None, f"Spotify falló: {exc}", False)


@tool()
def play_playlist(name: str) -> ToolResult:
    """Play a playlist by name from the user's Spotify library."""
    sp = _spotify()
    if sp is None:
        return _apple_music(
            f'tell application "Music" to play playlist "{name}"',
            f"Reproduciendo la lista {name} en Música.",
        )
    try:
        results = sp.current_user_playlists(limit=50)
        match = next(
            (p for p in results.get("items", []) if p["name"].lower() == name.lower()),
            None,
        )
        if match is None:
            return ToolResult(False, None, f"No tengo una lista llamada '{name}'.", False)
        sp.start_playback(context_uri=match["uri"])
        return ToolResult(True, {"uri": match["uri"]}, f"Reproduciendo {match['name']}.", False)
    except Exception as exc:
        return ToolResult(False, None, f"Spotify falló: {exc}", False)


def _spotify_transport(
    method: str, ok_msg: str, fallback_script: str, fallback_msg: str
) -> ToolResult:
    sp = _spotify()
    if sp is None:
        return _apple_music(fallback_script, fallback_msg)
    try:
        getattr(sp, method)()
    except Exception as exc:
        return ToolResult(False, None, f"Spotify falló: {exc}", False)
    return ToolResult(True, None, ok_msg, False)


@tool()
def pause() -> ToolResult:
    """Pause whatever is playing."""
    return _spotify_transport(
        "pause_playback", "Pausado.", 'tell application "Music" to pause', "Pausado."
    )


@tool()
def resume() -> ToolResult:
    """Resume playback."""
    return _spotify_transport(
        "start_playback", "Listo.", 'tell application "Music" to play', "Listo."
    )


@tool()
def next_track() -> ToolResult:
    """Skip to the next track."""
    return _spotify_transport(
        "next_track",
        "Siguiente.",
        'tell application "Music" to next track',
        "Siguiente.",
    )


@tool()
def previous_track() -> ToolResult:
    """Go to the previous track."""
    return _spotify_transport(
        "previous_track",
        "Anterior.",
        'tell application "Music" to previous track',
        "Anterior.",
    )


@tool()
def now_playing() -> ToolResult:
    """Tell the user what is currently playing."""
    sp = _spotify()
    if sp is None:
        try:
            name = macos.run_applescript(
                'tell application "Music" to name of current track & " - " & artist of current track'
            )
        except macos.AppleScriptError:
            return ToolResult(True, None, "No hay nada sonando.", False)
        return ToolResult(True, {"track": name}, f"Suena {name}.", False)
    try:
        cur = sp.current_playback()
        if not cur or not cur.get("item"):
            return ToolResult(True, None, "No hay nada sonando.", False)
        item = cur["item"]
        title = item["name"]
        artist = ", ".join(a["name"] for a in item.get("artists", []))
        return ToolResult(
            True,
            {"title": title, "artist": artist},
            f"Suena {title} de {artist}.",
            False,
        )
    except Exception as exc:
        return ToolResult(False, None, f"Spotify falló: {exc}", False)
