"""Unified install-time setup for Emma (Prompt 26.2).

    python -m emma.setup                 # TCC permissions + every OAuth service
    python -m emma.setup --only x        # just X (what `emma.x_setup` aliases to)
    python -m emma.setup --skip spotify  # everything except Spotify
    python -m emma.setup --skip-tcc      # OAuth only (installer already did TCC)
    python -m emma.setup --non-interactive   # CI: no prompts/browser, report gaps

Same philosophy as the TCC permissions convention (CLAUDE.md): every credential
and permission Emma needs is requested upfront at install, never as a surprise
"corre este comando" later. The installer calls this near the end.

Adding a future service (GitHub, LinkedIn, …) is two edits: implement the two
callables in the service's module —
    async run_*_setup(non_interactive: bool = False) -> bool   # runs the auth flow only
    <sync|async> *_token_status() -> "valid" | "expired" | "missing"   # reads Keychain, no prompt
— and append one entry to SERVICES below. The orchestrator owns all user-decision
logic ("¿vas a usar X?"); the callables never prompt and never raise on a missing
client id (they return False).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import time
from typing import Any

from config.settings import settings
from core import permissions

_STATE_PATH = settings.EMMA_HOME / "setup_state.json"

_X_GUIDE = f"""  Para X / Twitter, una sola vez:
    1. https://developer.x.com/en/portal/dashboard → crea un Project + App (free).
    2. User authentication settings → Native App, OAuth 2.0 ON,
       Redirect URI: {settings.X_REDIRECT_URI},
       Scopes: tweet.read tweet.write users.read offline.access.
    3. Pega el Client ID en .env como  X_CLIENT_ID=...  y vuelve a correr este comando."""

_SPOTIFY_GUIDE = """  Para Spotify, una sola vez:
    1. https://developer.spotify.com/dashboard → crea una app.
    2. Redirect URI: http://127.0.0.1:8888/callback.
    3. Pega Client ID y Client Secret en .env como SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET."""

SERVICES: list[dict[str, str]] = [
    {
        "name": "x",
        "label": "X / Twitter",
        "client_id_setting": "X_CLIENT_ID",
        "setup_callable": "core.x_oauth:run_pkce_setup",
        "token_status_callable": "core.x_oauth:token_status",
        "guide": _X_GUIDE,
    },
    {
        "name": "spotify",
        "label": "Spotify",
        "client_id_setting": "SPOTIFY_CLIENT_ID",
        "setup_callable": "tools.music:run_spotify_setup",
        "token_status_callable": "tools.music:spotify_token_status",
        "guide": _SPOTIFY_GUIDE,
    },
]

_YES = {"s", "si", "sí", "y", "yes"}


def _resolve(path: str) -> Any:
    mod, attr = path.split(":")
    return getattr(importlib.import_module(mod), attr)


async def _maybe_await(fn: Any, *args: Any, **kw: Any) -> Any:
    result = fn(*args, **kw)
    return await result if inspect.isawaitable(result) else result


def _load_state() -> dict[str, Any]:
    try:
        return dict(json.loads(_STATE_PATH.read_text(encoding="utf-8")))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _selected(args: argparse.Namespace) -> list[dict[str, str]]:
    out = []
    for svc in SERVICES:
        if args.only and svc["name"] not in args.only:
            continue
        if svc["name"] in args.skip:
            continue
        out.append(svc)
    return out


async def _setup_service(
    svc: dict[str, str], args: argparse.Namespace, state: dict[str, Any]
) -> str:
    name, label = svc["name"], svc["label"]
    forced = bool(args.only and name in args.only)

    # Respect a prior decline — don't re-pester unless explicitly --only'd (26.2-C3).
    if state.get(name, {}).get("status") == "skipped" and not forced:
        print(f"· {label}: lo saltaste antes (usa --only {name} para configurarlo).")
        return "skipped"

    status = await _maybe_await(_resolve(svc["token_status_callable"]))
    if status == "valid":
        print(f"✓ {label}: ya está configurado.")
        return "configured"

    client_id = getattr(settings, svc["client_id_setting"], "") or ""
    if not client_id:
        if args.non_interactive:
            print(f"· {label}: falta {svc['client_id_setting']} (pendiente).")
            return "pending"
        answer = input(f"¿Vas a usar {label}? (s/n) ").strip().lower()
        if answer not in _YES:
            print(f"· {label}: saltado.")
            return "skipped"
        print(svc["guide"])
        return "pending"

    ok = await _maybe_await(_resolve(svc["setup_callable"]), non_interactive=args.non_interactive)
    if ok:
        print(f"✓ {label}: autorizado.")
        return "configured"
    print(f"· {label}: quedó pendiente.")
    return "pending"


async def _run(args: argparse.Namespace) -> int:
    print("\n=== Configurando Emma ===  (esto toma 2-3 minutos)\n")

    if not args.skip_tcc and not args.only:
        print("→ Permisos del sistema (micrófono, automatización, etc.)")
        try:
            await permissions.bootstrap()
        except Exception as exc:
            print(f"⚠ Permisos: algunos quedaron pendientes ({exc}).")

    state = _load_state()
    results: dict[str, str] = {}
    for svc in _selected(args):
        results[svc["name"]] = await _setup_service(svc, args, state)
        state[svc["name"]] = {"status": results[svc["name"]], "last_attempt_ts": time.time()}
        _save_state(state)

    print("\n=== Resumen ===")
    for name, st in results.items():
        print(f"  {name}: {st}")
    pending = [n for n, st in results.items() if st == "pending"]
    if pending:
        print(f"\nPendientes: {', '.join(pending)}. Vuelve a correr cuando estén listos.")
        return 1
    print("\nTodo OK, no hace falta hacer nada más.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="emma.setup", description="Configura Emma (permisos + OAuth)."
    )
    ap.add_argument("--skip", default="", help="servicios a saltar, separados por coma")
    ap.add_argument("--only", default="", help="configura SOLO estos servicios (coma)")
    ap.add_argument("--non-interactive", action="store_true", help="sin prompts/navegador (CI)")
    ap.add_argument("--skip-tcc", action="store_true", help="omite los permisos TCC")
    args = ap.parse_args(argv)
    args.skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    args.only = {s.strip() for s in args.only.split(",") if s.strip()}
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
