"""The cascade's keys and voice (VOICE-1 Part 5): ElevenLabs required, OpenRouter optional.

Same rules as ``core.byok`` (the OpenAI key of the Realtime path), for the same
reason — **the backend never sees either key**:

* **Validated directly** against ElevenLabs / OpenRouter (hard-coded hosts),
  never through an Emma server.
* **Write-only.** Stored in Keychain under the frozen service; nothing here
  reads a key back except to build a masked hint (``sk_…a1b2``) or to call the
  provider that issued it.

The voice id is NOT a secret (Public tier): it lives in ``~/.emma/settings.env``,
the per-user settings file that survives reinstalls (``~/.emma/src`` doesn't).
``config.settings`` reads that file after ``.env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger("emma.voice_keys")

_ELEVENLABS_USER_URL = "https://api.elevenlabs.io/v1/user"
_ELEVENLABS_VOICES_URL = "https://api.elevenlabs.io/v2/voices"
_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"

USER_SETTINGS = Path.home() / ".emma" / "settings.env"

ELEVENLABS = "ELEVENLABS_API_KEY"
OPENROUTER = "OPENROUTER_API_KEY"


def looks_well_formed(key: str) -> bool:
    key = (key or "").strip()
    return len(key) >= 20 and " " not in key


def masked(key: str) -> str:
    key = (key or "").strip()
    return f"{key[:3]}…{key[-4:]}" if len(key) >= 12 else "…"


async def _get(url: str, headers: dict[str, str], timeout_s: float) -> int | None:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            return (await client.get(url, headers=headers)).status_code
    except Exception as exc:
        # Never the exception text: it can carry the URL and, via proxies, headers.
        log.warning("voice_key_network_error", error_type=type(exc).__name__)
        return None


async def validate_elevenlabs(key: str, *, timeout_s: float = 12.0) -> tuple[bool, str]:
    """Ask ElevenLabs directly. Returns (ok, message in Spanish). Never raises."""
    key = (key or "").strip()
    if not looks_well_formed(key):
        return False, "Esa key no tiene el formato correcto."
    status = await _get(_ELEVENLABS_USER_URL, {"xi-api-key": key}, timeout_s)
    if status is None:
        return False, "No pude conectarme a ElevenLabs. Revisa tu conexión e intenta de nuevo."
    if status == 200:
        return True, "Key verificada."
    if status in (401, 403):
        return False, "ElevenLabs rechazó esa key. Revísala y vuelve a intentar."
    return False, f"ElevenLabs respondió {status}. Intenta de nuevo en un momento."


async def validate_openrouter(key: str, *, timeout_s: float = 12.0) -> tuple[bool, str]:
    key = (key or "").strip()
    if not looks_well_formed(key):
        return False, "Esa key no tiene el formato correcto."
    status = await _get(_OPENROUTER_KEY_URL, {"Authorization": f"Bearer {key}"}, timeout_s)
    if status is None:
        return False, "No pude conectarme a OpenRouter. Revisa tu conexión e intenta de nuevo."
    if status == 200:
        return True, "Key guardada. Todavía no se usa."
    if status in (401, 403):
        return False, "OpenRouter rechazó esa key. Revísala y vuelve a intentar."
    return False, f"OpenRouter respondió {status}. Intenta de nuevo en un momento."


async def store(label: str, key: str) -> None:
    """Keychain, under the frozen service, and nowhere else."""
    if label not in (ELEVENLABS, OPENROUTER):
        raise ValueError(f"not a cascade key: {label}")
    from core import secrets

    await secrets.store(label, key.strip(), kind="secret")
    log.info("voice_key_stored", label=label, key=masked(key))


async def installed_hint(label: str) -> str | None:
    from core import secrets

    value = await secrets.retrieve(label)
    return masked(value) if value else None


@dataclass(frozen=True)
class Voice:
    voice_id: str
    name: str
    description: str  # "female · american · conversational", from the provider's labels

    @property
    def label(self) -> str:
        return f"{self.name} — {self.description}" if self.description else self.name


async def list_voices(key: str, *, timeout_s: float = 12.0) -> list[Voice]:
    """The voices this ElevenLabs account can use, by name. [] on any failure."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.get(_ELEVENLABS_VOICES_URL, params={"page_size": 100},
                                 headers={"xi-api-key": key.strip()})
        if r.status_code != 200:
            return []
        voices = r.json().get("voices", [])
    except Exception as exc:
        log.warning("voice_list_failed", error_type=type(exc).__name__)
        return []
    out = []
    for v in voices:
        labels = v.get("labels") or {}
        desc = " · ".join(str(labels[k]) for k in ("gender", "accent", "use_case") if labels.get(k))
        out.append(Voice(str(v["voice_id"]), str(v.get("name", "")).strip(), desc))
    return sorted(out, key=lambda v: v.name.lower())


def save_voice(voice_id: str, path: Path | None = None) -> None:
    """Persist VOICE_ID in the per-user settings file (0600, atomic), keeping other lines."""
    voice_id = voice_id.strip()
    if not voice_id or any(c.isspace() or c in "=#\"'" for c in voice_id):
        raise ValueError("not a voice id")
    target = path or USER_SETTINGS
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if target.exists():
        lines = [ln for ln in target.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith("VOICE_ID=")]
    lines.append(f"VOICE_ID={voice_id}")
    tmp = target.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(target)
    from config.settings import settings

    object.__setattr__(settings, "VOICE_ID", voice_id)
    log.info("voice_chosen", voice_id=voice_id)


def saved_voice(path: Path | None = None) -> str:
    """VOICE_ID from the per-user settings file ("" if none) — re-read while parked."""
    target = path or USER_SETTINGS
    try:
        for ln in target.read_text(encoding="utf-8").splitlines():
            if ln.startswith("VOICE_ID="):
                return ln.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""
