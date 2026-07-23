"""Music control: Spotify Web API when configured, Apple Music fallback.

Spotify path: spotipy drives an Auth-Code-with-PKCE flow on first run
(the prompt called it "device flow" - that's not what Spotify supports;
this is the closest analog). The browser opens once for consent and the
refresh token is cached at ``~/.emma/spotify_token.json``.

If `SPOTIFY_CLIENT_ID`/`SECRET` are missing, every Spotify call returns a
clean failure and the AppleScript-driven Apple Music path takes over.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import Any

import structlog

from actions import macos
from config.settings import settings
from core import app_router
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.music")


def _music_app() -> str:
    """The music app to drive RIGHT NOW (22-B30): frontmost wins, then whatever
    is actually running (preference breaks ties), then preference, then Music.
    Thin wrapper kept for the existing call sites; the router is the truth."""
    return app_router.preferred("music")


# Module-level tuple so scope drift is visible in one place (22.1-B37).
# playlist-read-* closes the live 403 from ERRORS-TO-FIX §6.
_SCOPE_TUPLE = (
    "user-modify-playback-state",
    "user-read-playback-state",
    "user-read-currently-playing",
    "playlist-read-private",
    "playlist-read-collaborative",
)
_SCOPES = " ".join(_SCOPE_TUPLE)
_spotify_client: Any = None
_spotify_unavailable = False


def _scopes_hash() -> str:
    import hashlib

    return hashlib.sha256("|".join(_SCOPE_TUPLE).encode()).hexdigest()[:16]


def _evict_token_on_scope_drift() -> bool:
    """True if the cached token was minted for a DIFFERENT scope set.

    Spotify token refreshes never widen scopes — only a fresh consent does
    (developer.spotify.com/documentation/web-api/concepts/scopes). We store
    the scope-set hash beside the token; on drift, evict so the PKCE flow
    re-runs with the new scopes.
    """
    token_path = settings.EMMA_HOME / "spotify_token.json"
    hash_path = settings.EMMA_HOME / "spotify_scopes.hash"
    current = _scopes_hash()
    stored = hash_path.read_text().strip() if hash_path.exists() else ""
    if stored == current:
        return False
    if token_path.exists():
        token_path.unlink()
        log.warning("spotify_scope_drift", old=stored or "<none>", new=current)
    hash_path.parent.mkdir(parents=True, exist_ok=True)
    hash_path.write_text(current)
    return bool(stored)  # drift only counts if there WAS a previous grant


def _have_spotify_creds() -> bool:
    return bool(settings.SPOTIFY_CLIENT_ID and settings.SPOTIFY_CLIENT_SECRET)


def _spotify_cache_path() -> Any:
    path = settings.EMMA_HOME / "spotify_token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _spotify_auth() -> Any:
    """Build the SpotifyOAuth manager (shared by the lazy + eager-setup paths).
    Raises if spotipy is unavailable; callers handle that."""
    from spotipy.oauth2 import SpotifyOAuth

    if _evict_token_on_scope_drift():
        # The next call runs the PKCE consent in the browser — the user
        # hears about it instead of a mystery hang (22.1-B37).
        log.info("spotify_reauth_needed", reason="scope set widened (playlists)")
    return SpotifyOAuth(
        client_id=settings.SPOTIFY_CLIENT_ID,
        client_secret=settings.SPOTIFY_CLIENT_SECRET,
        redirect_uri=settings.SPOTIFY_REDIRECT_URI,
        scope=_SCOPES,
        cache_path=str(_spotify_cache_path()),
        open_browser=True,
    )


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

        _spotify_client = spotipy.Spotify(auth_manager=_spotify_auth())
        return _spotify_client
    except Exception as exc:
        log.error("spotify_init_failed", error=str(exc))
        _spotify_unavailable = True
        return None


def spotify_token_status() -> str:
    """'valid' | 'expired' | 'missing' for the cached Spotify token (26.2).
    Reads ~/.emma/spotify_token.json without prompting."""
    import json
    import time

    path = settings.EMMA_HOME / "spotify_token.json"
    if not path.exists():
        return "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "missing"
    return "valid" if float(data.get("expires_at", 0)) > time.time() else "expired"


async def run_spotify_setup(non_interactive: bool = False) -> bool:
    """Eagerly run the Spotify PKCE consent at install time (26.2). True once a
    token is cached. The lazy path in _spotify() is unchanged."""
    if not _have_spotify_creds() or non_interactive:
        return False

    def _authorize() -> bool:
        try:
            auth = _spotify_auth()
            # check_cache=True reuses a valid token; otherwise opens the browser
            # and blocks on spotipy's local redirect server until authorized.
            token = auth.get_access_token(as_dict=False, check_cache=True)
            return bool(token)
        except Exception as exc:
            log.error("spotify_setup_failed", error=str(exc))
            return False

    return await asyncio.to_thread(_authorize)


def _missing_creds_result() -> ToolResult:
    return ToolResult(
        False,
        None,
        "No tengo credenciales de Spotify configuradas todavía. Usaré Música si lo intentas de nuevo.",
        False,
    )


def _apple_music(
    script: str, ok_msg: str, *, app: str = "", ends_session: bool = False
) -> ToolResult:
    try:
        macos.run_applescript(script)
    except macos.AppleScriptError as exc:
        data = None
        if app and ("-600" in str(exc) or "isn't running" in str(exc)):
            # OS-state mismatch (22-B33): structured so the LLM proposes a fix.
            data = app_router.failure_data("music", app, "app_not_running")
        return ToolResult(False, data, f"Música no respondió: {exc}", False)
    return ToolResult(True, None, ok_msg, False, ends_session=ends_session)


def _resolve_music_or_ask(explicit: str) -> tuple[str | None, ToolResult | None]:
    """(app, None) to proceed, or (None, ask) when NOTHING music-ish is open.

    22-B33: with neither Spotify nor Music running, launching one silently
    guesses — Emma asks instead ("¿Abro Spotify o uso Music?"). the user's
    pick comes back as the explicit ``app`` arg, which always proceeds.
    """
    if explicit:
        return explicit.strip(), None
    decision = app_router.inspect("music")
    if decision.source in ("frontmost", "running"):
        return decision.picked, None
    data = app_router.failure_data("music", decision.picked, "app_not_running")
    alts = [a for a in data["alternatives"] if a]
    alt_txt = f" o uso {alts[0]}" if alts else ""
    return None, ToolResult(
        False,
        data,
        f"No tienes ninguna app de música abierta. ¿Abro {decision.picked}{alt_txt}?",
        False,
    )


async def _ensure_running(app: str) -> None:
    """Launch ``app`` if it isn't already running, so AppleScript transport lands
    on a live app (Bug 19.2-B3 — fixes Spotify "no active device")."""
    if not await macos.app_is_running(app):
        await macos.launch_app(app)


def _spotify_search_uri(query: str) -> str | None:
    """Resolve a free-text query to a Spotify track URI via the Web API (search
    only — playback goes through AppleScript). None if unavailable / no match."""
    sp = _spotify()
    if sp is None:
        return None
    try:
        items = sp.search(q=query, type="track", limit=1).get("tracks", {}).get("items", [])
        return items[0]["uri"] if items else None
    except Exception as exc:
        log.error("spotify_search_failed", error=str(exc))
        return None


def _spotify_playlist_uri(name: str) -> str | None:
    """Resolve a playlist name to its Spotify URI via the Web API. None if N/A."""
    sp = _spotify()
    if sp is None:
        return None
    try:
        items = sp.current_user_playlists(limit=50).get("items", [])
        match = next((p for p in items if p["name"].lower() == name.lower()), None)
        return match["uri"] if match else None
    except Exception as exc:
        log.error("spotify_playlist_lookup_failed", error=str(exc))
        return None


@tool()
async def play_track(query: str, app: str = "") -> ToolResult:
    """Search for a track or artist and start playing the top result.

    Routes to whatever music app is actually open (22-B30); with none open
    it ASKS which to launch — the user's pick comes back as `app="Spotify"` /
    `app="Music"`, which always proceeds."""
    picked, ask = _resolve_music_or_ask(app)
    if ask is not None:
        return ask
    assert picked is not None
    app = picked
    await _ensure_running(app)
    if app == "Spotify":
        uri = _spotify_search_uri(query)
        if uri:
            esc = macos.esc_applescript(uri)
            return _apple_music(
                f'tell application "Spotify" to play track "{esc}"',
                f"Reproduciendo {query} en Spotify.",
                app="Spotify",
                ends_session=True,
            )
        # No Spotify search available → fall back to Apple Music (always installed).
        app = "Music"
        await _ensure_running(app)
    q = macos.esc_applescript(query)
    return _apple_music(
        f'tell application "{macos.esc_applescript(app)}" to '
        f'(play (first track of (search library 1 for "{q}")))',
        f"Reproduciendo {query} en {app}.",
        app=app,
        ends_session=True,
    )


@tool()
async def play_playlist(name: str, app: str = "") -> ToolResult:
    """Play a playlist by name. Routes to the OPEN music app; with none open
    it asks which to launch (re-call with app=<pick>)."""
    picked, ask = _resolve_music_or_ask(app)
    if ask is not None:
        return ask
    assert picked is not None
    app = picked
    await _ensure_running(app)
    if app == "Spotify":
        uri = _spotify_playlist_uri(name)
        if uri:
            esc = macos.esc_applescript(uri)
            return _apple_music(
                f'tell application "Spotify" to play track "{esc}"',
                f"Reproduciendo la lista {name} en Spotify.",
                app="Spotify",
                ends_session=True,
            )
        app = "Music"
        await _ensure_running(app)
    n = macos.esc_applescript(name)
    result = _apple_music(
        f'tell application "{macos.esc_applescript(app)}" to play playlist "{n}"',
        f"Reproduciendo la lista {name} en {app}.",
        app=app,
        ends_session=True,
    )
    if not result.success and "-1700" in (result.user_message or ""):
        # 22.1-B37: -1700 = the playlist isn't a LIBRARY playlist (editorial
        # ones like "Classical Essentials" live in Apple Music's catalog, not
        # the local library — ERRORS-TO-FIX §6). Open the catalog search via
        # the music:// URL scheme instead of dying with a type error.
        return await _open_music_search(name)
    return result


async def _open_music_search(term: str) -> ToolResult:
    """Open Apple Music's catalog search for ``term`` (22.1-B37 fallback)."""
    url = f"music://music.apple.com/search?term={urllib.parse.quote_plus(term)}"
    try:
        proc = await asyncio.create_subprocess_exec("open", url, stdout=None, stderr=None)
        await proc.wait()
        ok = proc.returncode == 0
    except Exception:
        ok = False
    if ok:
        return ToolResult(
            True,
            {"failure_reason": "playlist_not_in_library", "playlist": term, "opened_search": True},
            f"Esa playlist no está en tu biblioteca; te abrí la búsqueda de "
            f"'{term}' en Apple Music. Dale 'agregar' si la quieres guardada.",
            False,
        )
    return ToolResult(
        False,
        {"failure_reason": "playlist_not_in_library", "playlist": term},
        f"No tengo la playlist '{term}' guardada. ¿La busco en Apple Music "
        "o te la guardo a tu biblioteca?",
        False,
    )


async def _transport(verb: str, ok_msg: str, *, ends_session: bool = False) -> ToolResult:
    """Run a transport verb (pause/play/next track/previous track) on whatever
    music app is OPEN (22-B30). Launching an app just to pause it is absurd —
    with nothing running, say so with structured data instead (22-B33)."""
    decision = app_router.inspect("music")
    if decision.source not in ("frontmost", "running"):
        data = app_router.failure_data("music", decision.picked, "app_not_running")
        return ToolResult(False, data, "No hay ninguna app de música abierta.", False)
    app = decision.picked
    return _apple_music(
        f'tell application "{macos.esc_applescript(app)}" to {verb}',
        ok_msg,
        app=app,
        ends_session=ends_session,
    )


@tool()
async def pause() -> ToolResult:
    """Pause whatever is playing."""
    # pause stops the audio, so the mic no longer fights it — keep listening.
    return await _transport("pause", "Pausado.")


@tool()
async def resume() -> ToolResult:
    """Resume playback."""
    return await _transport("play", "Listo.", ends_session=True)


@tool()
async def next_track() -> ToolResult:
    """Skip to the next track."""
    return await _transport("next track", "Siguiente.", ends_session=True)


@tool()
async def previous_track() -> ToolResult:
    """Go to the previous track."""
    return await _transport("previous track", "Anterior.", ends_session=True)


@tool()
def now_playing() -> ToolResult:
    """Tell the user what is currently playing."""
    sp = _spotify()
    if sp is None:
        try:
            name = macos.run_applescript(
                f'tell application "{_music_app()}" to name of current track '
                '& " - " & artist of current track'
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
