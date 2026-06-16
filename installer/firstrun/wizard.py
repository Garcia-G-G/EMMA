"""First-run wizard for Emma (Prompt 29, Part C).

After install, Emma.app launches with ``--first-run`` and opens this wizard in the
user's browser — the spec's local-HTML fallback to a fragile native PyObjC window.
It serves five Spanish steps on 127.0.0.1:8724 and exposes a small JSON API that
wires into the REAL subsystems:

  - TCC permission checks  → core.permissions.check_*
  - API keys → Keychain    → core.secrets.store  (NEVER .env)
  - external OAuth services → emma.setup (the 26.2 orchestrator) in a subprocess
  - voice/mic test         → sounddevice capture + RMS

The API functions below are importable + pure so they're unit-testable without a
browser; the HTTP handler is a thin delegator over them.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

HOST = "127.0.0.1"
PORT = 8724
_HTML = Path(__file__).with_name("wizard.html")
_ALLOWED_SERVICES = {"x", "spotify", "linear", "jira", "notion"}


# ---- API logic (importable, testable) ---------------------------------------


def check_permissions() -> dict[str, bool]:
    """Live TCC status for each pane Emma needs. Never raises."""
    from core import permissions

    checks = {
        "microphone": permissions.check_microphone,
        "accessibility": permissions.check_accessibility,
        "calendar": permissions.check_calendar,
        "automation": permissions.check_automation,
    }
    out: dict[str, bool] = {}
    for name, fn in checks.items():
        try:
            out[name] = bool(fn())
        except Exception:
            out[name] = False
    return out


async def validate_openai_key(key: str) -> bool:
    """True if the key authenticates against OpenAI's /models endpoint."""
    if not key.strip():
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key.strip()}"},
            )
        return r.status_code == 200
    except Exception:
        return False


async def save_keys(openai_key: str, elevenlabs_key: str = "") -> dict[str, Any]:
    """Validate the OpenAI key, then store both keys in the Keychain (not .env)."""
    from core import secrets

    if not await validate_openai_key(openai_key):
        return {"ok": False, "error": "La llave de OpenAI no es válida."}
    await secrets.store("OPENAI_API_KEY", openai_key.strip(), kind="secret")
    if elevenlabs_key.strip():
        await secrets.store("ELEVENLABS_API_KEY", elevenlabs_key.strip(), kind="secret")
    return {"ok": True}


def run_service_setup(name: str) -> dict[str, Any]:
    """Delegate one external service to the 26.2 orchestrator (emma.setup)."""
    if name not in _ALLOWED_SERVICES:
        return {"ok": False, "error": f"servicio desconocido: {name}"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "emma.setup", "--only", name, "--skip-tcc"],
            capture_output=True, text=True, timeout=180,
        )
        return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr)[-2000:]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def mic_test(seconds: float = 1.2) -> dict[str, Any]:
    """Record a moment of audio and confirm the mic is actually capturing signal."""
    try:
        import numpy as np
        import sounddevice as sd

        rec = sd.rec(int(seconds * 16000), samplerate=16000, channels=1, dtype="int16")
        sd.wait()
        rms = float(np.sqrt(np.mean(rec.astype("float64") ** 2)))
        return {"ok": rms > 1.0, "rms": round(rms, 1)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---- HTTP server (thin delegator) -------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return dict(json.loads(self.rfile.read(n).decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, _HTML.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/permissions":
            self._json(check_permissions())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        body = self._body()
        if self.path == "/api/keys":
            self._json(asyncio.run(save_keys(body.get("openai", ""), body.get("elevenlabs", ""))))
        elif self.path == "/api/service":
            self._json(run_service_setup(str(body.get("name", ""))))
        elif self.path == "/api/voicetest":
            self._json(mic_test())
        elif self.path == "/api/done":
            self._json({"ok": True})
            self.server.shutdown()
        else:
            self._json({"error": "not found"}, 404)


def run(open_browser: bool = True) -> None:
    """Serve the wizard and (optionally) open it in the browser. Blocks until done."""
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    url = f"http://{HOST}:{PORT}/"
    if open_browser:
        webbrowser.open(url)
    print(f"Asistente de configuración de Emma → {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
