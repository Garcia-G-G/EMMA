"""Environment detection, install guidance, and system-default wiring.

Two operating modes (see phase-06 spec):

- "Open for the user" actions never call into this module - they use
  ``open <thing>`` and macOS routes to the system default.
- "Automate / execute" actions (open Emma's source for editing, open a
  terminal she will pre-populate, drive music playback) call
  :func:`detect_preferred` to pick a concrete app.

Detection results plus user voice-overrides are persisted to
``~/.emma/environment_cache.json``. The cache has a 24h TTL; user
overrides have no TTL and beat detection.

# TODO(phase-03): mirror preferences into long-term memory once that
#   substrate exists. JSON is the local source of truth until then.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

from config.settings import settings
from core.tts import say_fallback

log = structlog.get_logger("emma.environment")

Category = Literal["ide", "terminal", "music", "browser"]
CACHE_TTL_S = 24 * 60 * 60
CACHE_PATH = settings.EMMA_HOME / "environment_cache.json"


# Shortlists are the contract. Order = preference. Do not extend without
# updating the phase-06 spec first.
IDE_SHORTLIST: list[dict[str, Any]] = [
    {
        "key": "cursor",
        "binary": "cursor",
        "apps": ["Cursor"],
        "bundle": "com.todesktop.230313mzl4w4u92",
    },
    {
        "key": "code",
        "binary": "code",
        "apps": ["Visual Studio Code"],
        "bundle": "com.microsoft.VSCode",
    },
    {"key": "zed", "binary": "zed", "apps": ["Zed"], "bundle": "dev.zed.Zed"},
    {"key": "subl", "binary": "subl", "apps": ["Sublime Text"], "bundle": "com.sublimetext.4"},
]

TERMINAL_SHORTLIST: list[dict[str, Any]] = [
    {"key": "iterm", "apps": ["iTerm"], "bundle": "com.googlecode.iterm2"},
    {"key": "warp", "apps": ["Warp"], "bundle": "dev.warp.Warp-Stable"},
    {"key": "ghostty", "apps": ["Ghostty"], "bundle": "com.mitchellh.ghostty"},
    {"key": "terminal", "apps": ["Terminal"], "bundle": "com.apple.Terminal"},  # always present
]

MUSIC_SHORTLIST: list[dict[str, Any]] = [
    {"key": "spotify", "apps": ["Spotify"], "bundle": "com.spotify.client"},
    {"key": "music", "apps": ["Music"], "bundle": "com.apple.Music"},  # always present
]

BROWSER_SHORTLIST: list[dict[str, Any]] = [
    {
        "key": "brave",
        "apps": ["Brave Browser"],
        "bundle": "com.brave.Browser",
        "cask": "brave-browser",
    },
    {
        "key": "chrome",
        "apps": ["Google Chrome"],
        "bundle": "com.google.Chrome",
        "cask": "google-chrome",
    },
    {"key": "firefox", "apps": ["Firefox"], "bundle": "org.mozilla.firefox", "cask": "firefox"},
    {"key": "arc", "apps": ["Arc"], "bundle": "company.thebrowser.Browser", "cask": "arc"},
    {"key": "safari", "apps": ["Safari"], "bundle": "com.apple.Safari"},  # always present
]

SHORTLISTS: dict[Category, list[dict[str, Any]]] = {
    "ide": IDE_SHORTLIST,
    "terminal": TERMINAL_SHORTLIST,
    "music": MUSIC_SHORTLIST,
    "browser": BROWSER_SHORTLIST,
}

INSTALL_RECOMMENDATIONS: dict[Category, dict[str, str]] = {
    "ide": {"key": "code", "cask": "visual-studio-code", "human": "VS Code"},
    "terminal": {"key": "ghostty", "cask": "ghostty", "human": "Ghostty"},
    "music": {"key": "spotify", "cask": "spotify", "human": "Spotify"},
}

# Extensions duti rewrites when an IDE is installed. The UTI line covers
# Python scripts; the per-extension calls cover the rest.
DUTI_EXTENSIONS: list[str] = [
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".rb",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".md",
    ".txt",
    ".sh",
    ".html",
    ".css",
    ".sql",
    ".env",
]
DUTI_UTIS: list[str] = ["public.python-script"]


@dataclass(frozen=True)
class DetectionResult:
    app_name: str | None
    bundle_id: str | None
    binary_path: str | None
    available_alternatives: list[str] = field(default_factory=list)
    is_user_override: bool = False


# ---------- cache I/O ----------------------------------------------------


def _load_state() -> dict[str, Any]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception as exc:
        log.warning("env_cache_load_failed", error=str(exc))
        return {}


def _save_state(state: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(CACHE_PATH)


def _fresh(state: dict[str, Any]) -> bool:
    ts = state.get("detected_at")
    if not isinstance(ts, (int, float)):
        return False
    return (time.time() - float(ts)) < CACHE_TTL_S


# ---------- low-level probes --------------------------------------------


def _app_installed(app_names: list[str]) -> bool:
    roots = (
        "/Applications",
        str(Path.home() / "Applications"),
        "/System/Applications",
        "/System/Applications/Utilities",
    )
    for root in roots:
        for app in app_names:
            if (Path(root) / f"{app}.app").exists():
                return True
    return False


def _entry_installed(entry: dict[str, Any]) -> tuple[bool, str | None]:
    """Return (installed, binary_path)."""
    binary_name = entry.get("binary")
    binary_path = shutil.which(binary_name) if binary_name else None
    if binary_path:
        return True, binary_path
    if _app_installed(entry.get("apps", [])):
        return True, None
    return False, None


def _detect_category(category: Category) -> tuple[str | None, str | None, str | None, list[str]]:
    """Probe shortlist. Returns (chosen_key, bundle_id, binary_path, available_keys)."""
    chosen_key: str | None = None
    chosen_bundle: str | None = None
    chosen_bin: str | None = None
    available: list[str] = []
    for entry in SHORTLISTS[category]:
        installed, binpath = _entry_installed(entry)
        if installed:
            available.append(entry["key"])
            if chosen_key is None:
                chosen_key = entry["key"]
                chosen_bundle = entry.get("bundle")
                chosen_bin = binpath
    return chosen_key, chosen_bundle, chosen_bin, available


# ---------- public detection API -----------------------------------------


def detect_preferred(category: Category, *, force_refresh: bool = False) -> DetectionResult:
    """Resolve the active app for ``category``.

    Order: user override (validated still installed) > 24h cache > fresh
    probe > None.
    """
    state = _load_state()
    overrides: dict[str, str] = state.get("preferences", {})
    override_key = overrides.get(category)

    if override_key:
        entry = next((e for e in SHORTLISTS[category] if e["key"] == override_key), None)
        if entry is not None:
            installed, binpath = _entry_installed(entry)
            if installed:
                _, _, _, available = _detect_category(category)
                return DetectionResult(
                    app_name=override_key,
                    bundle_id=entry.get("bundle"),
                    binary_path=binpath,
                    available_alternatives=available,
                    is_user_override=True,
                )
            # Override no longer installed: fall through to detection.

    if not force_refresh:
        detection = state.get("detection", {}).get(category)
        if detection and _fresh(state):
            return DetectionResult(
                app_name=detection.get("app_name"),
                bundle_id=detection.get("bundle_id"),
                binary_path=detection.get("binary_path"),
                available_alternatives=detection.get("available_alternatives", []),
                is_user_override=False,
            )

    chosen, bundle, binpath, available = _detect_category(category)
    result = DetectionResult(
        app_name=chosen,
        bundle_id=bundle,
        binary_path=binpath,
        available_alternatives=available,
        is_user_override=False,
    )

    state.setdefault("detection", {})[category] = asdict(result)
    state["detected_at"] = time.time()
    _save_state(state)
    return result


def warm_cache() -> None:
    """Run all detections; call once at startup. Idempotent."""
    for cat in ("ide", "terminal", "music"):
        detect_preferred(cat)  # type: ignore[arg-type]


# ---------- preference + decline tracking --------------------------------


def get_preference(category: Category) -> str | None:
    return _load_state().get("preferences", {}).get(category)


def set_preference(category: Category, key: str) -> None:
    state = _load_state()
    state.setdefault("preferences", {})[category] = key
    _save_state(state)


def record_decline(category: Category) -> int:
    state = _load_state()
    declines = state.setdefault("declines", {})
    declines[category] = int(declines.get(category, 0)) + 1
    _save_state(state)
    return int(declines[category])


def decline_count(category: Category) -> int:
    return int(_load_state().get("declines", {}).get(category, 0))


# ---------- Homebrew + duti ----------------------------------------------


def have_brew() -> bool:
    return shutil.which("brew") is not None


def have_duti() -> bool:
    return shutil.which("duti") is not None


def brew_install_one_liner() -> str:
    return (
        '/bin/bash -c "$(curl -fsSL '
        'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    )


async def _run_with_progress(
    cmd: list[str],
    spoken_lang: str,
    progress_es: str = "Sigo instalando…",
    progress_en: str = "Still installing…",
    every_s: int = 10,
) -> tuple[int, str]:
    """Spawn ``cmd`` and speak a progress phrase every ``every_s`` seconds."""
    log.info("subprocess_start", cmd=cmd[:2])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def _heartbeat() -> None:
        try:
            while True:
                await asyncio.sleep(every_s)
                if proc.returncode is None:
                    say_fallback(progress_es if spoken_lang == "es" else progress_en, spoken_lang)  # type: ignore[arg-type]
        except asyncio.CancelledError:
            pass

    beat = asyncio.create_task(_heartbeat())
    try:
        stdout, _ = await proc.communicate()
    finally:
        beat.cancel()
    return proc.returncode or 0, stdout.decode(errors="replace")


async def install_cask(cask: str, spoken_lang: str = "es") -> tuple[bool, str]:
    if not have_brew():
        return False, "brew is not installed"
    code, out = await _run_with_progress(
        ["brew", "install", "--cask", cask], spoken_lang=spoken_lang
    )
    log.info("brew_cask_result", cask=cask, code=code, tail=out[-400:])
    return code == 0, out


async def install_brew_package(name: str, spoken_lang: str = "es") -> tuple[bool, str]:
    if not have_brew():
        return False, "brew is not installed"
    code, out = await _run_with_progress(["brew", "install", name], spoken_lang=spoken_lang)
    return code == 0, out


async def ensure_duti(spoken_lang: str = "es") -> bool:
    if have_duti():
        return True
    ok, _ = await install_brew_package("duti", spoken_lang=spoken_lang)
    return ok and have_duti()


def set_ide_default(bundle_id: str) -> dict[str, Any]:
    """Point duti's per-extension and UTI handlers at ``bundle_id``.

    Returns a summary dict with the verification result for
    ``public.python-script`` (the headline UTI we read back to confirm).
    """
    if not have_duti():
        return {"ok": False, "error": "duti missing"}
    results: dict[str, int] = {}
    for ext in DUTI_EXTENSIONS:
        proc = subprocess.run(
            ["duti", "-s", bundle_id, ext, "all"],
            capture_output=True,
            text=True,
        )
        results[ext] = proc.returncode
    for uti in DUTI_UTIS:
        proc = subprocess.run(
            ["duti", "-s", bundle_id, uti, "all"],
            capture_output=True,
            text=True,
        )
        results[uti] = proc.returncode
    # Verify via -d on python-script
    verify = subprocess.run(
        ["duti", "-d", "public.python-script"],
        capture_output=True,
        text=True,
    )
    return {
        "ok": all(rc == 0 for rc in results.values()) and bundle_id in verify.stdout,
        "verify_bundle": verify.stdout.strip(),
        "expected_bundle": bundle_id,
    }


def smoke_launch(app_name: str) -> bool:
    """Launch and quickly close an app to warm Gatekeeper / Launch Services."""
    try:
        subprocess.run(["open", "-a", app_name], check=True, timeout=20)
        time.sleep(2)
        return True
    except Exception as exc:
        log.warning("smoke_launch_failed", app=app_name, error=str(exc))
        return False


# ---------- D-bis: browser default (system dialog unavoidable) -----------


def default_browser_bundle() -> str | None:
    """Read the current default browser bundle id from LaunchServices."""
    try:
        proc = subprocess.run(
            [
                "defaults",
                "read",
                "com.apple.LaunchServices/com.apple.launchservices.secure",
                "LSHandlers",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    # LSHandlers is an array; find the first https handler.
    text = proc.stdout
    chunks = text.split("LSHandlerURLScheme")
    for chunk in chunks:
        if "https" in chunk:
            # Look for LSHandlerRoleAll = "<bundle>"
            for line in chunk.splitlines():
                line = line.strip().rstrip(";")
                if line.startswith("LSHandlerRoleAll"):
                    return line.split("=", 1)[1].strip().strip('"')
    return None


async def trigger_default_browser_change(bundle_id: str) -> None:
    """Open the target browser - its own first-run prompt will offer to
    become default. macOS will show the confirmation dialog; we can't
    bypass that. After the call, ``default_browser_bundle()`` can be
    polled to see whether the user clicked through.
    """
    subprocess.run(["open", "-b", bundle_id], check=False)
