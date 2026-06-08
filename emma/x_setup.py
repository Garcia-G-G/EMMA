"""One-time X / Twitter authorization for Emma (Prompt 26.1).

    python -m emma.x_setup

Runs the OAuth 2.0 PKCE consent in the browser, then stores the access +
refresh tokens in the macOS Keychain so ``post_to_x`` can post for real,
indefinitely (auto-refreshing on expiry). CLIENT_ID is public (PKCE) and lives
in .env; the tokens are secret and never leave the Keychain.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import urllib.parse
from typing import Any

from config.settings import settings
from core import secrets, x_oauth

_GUIDE = """
Primero necesito tu X_CLIENT_ID. Una sola vez:

  1. Entra a https://developer.x.com/en/portal/dashboard y crea un Project + App
     (el free tier sirve).
  2. En la App → "User authentication settings":
       - Tipo de app: Native App  (cliente público / PKCE)
       - OAuth 2.0: activado
       - Callback / Redirect URI:  {redirect}
       - Scopes:  tweet.read  tweet.write  users.read  offline.access
  3. Copia el "Client ID" y pégalo en tu archivo .env como:
       X_CLIENT_ID=tu_client_id_aqui
  4. Vuelve a correr:  python -m emma.x_setup
"""


async def _store_tokens(tokens: dict[str, Any]) -> None:
    access = tokens.get("access_token")
    if not access:
        raise ValueError("X did not return an access_token")
    await secrets.store("X_ACCESS_TOKEN", access, kind="oauth_token")
    if tokens.get("refresh_token"):
        await secrets.store("X_REFRESH_TOKEN", tokens["refresh_token"], kind="oauth_token")
    expires_at = int(time.time()) + int(tokens.get("expires_in", 7200))
    await secrets.store("X_TOKEN_EXPIRES_AT", str(expires_at), kind="oauth_meta")


def main() -> int:
    print("\n=== Configuración de X / Twitter para Emma ===\n")

    if not settings.X_CLIENT_ID:
        print(_GUIDE.format(redirect=settings.X_REDIRECT_URI))
        return 1

    verifier, challenge = x_oauth.make_pkce_pair()
    state = x_oauth.make_state()
    url = x_oauth.build_authorize_url(
        settings.X_CLIENT_ID, settings.X_REDIRECT_URI, settings.X_SCOPES, challenge, state
    )
    port = urllib.parse.urlparse(settings.X_REDIRECT_URI).port or 8723

    print("Abriendo X.com para que autorices a Emma…")
    print(f"(Si no se abre solo, pega esto en tu navegador:\n  {url}\n)")
    subprocess.run(["open", url], check=False)

    try:
        cb = x_oauth.run_callback_server(state, port=port)
    except TimeoutError:
        print("\n✗ No recibí la autorización a tiempo. Vuelve a correr el comando.")
        return 1
    except Exception as exc:
        print(f"\n✗ La autorización falló: {exc}")
        return 1

    try:
        tokens = asyncio.run(
            x_oauth.exchange_code(
                settings.X_CLIENT_ID, cb["code"], verifier, settings.X_REDIRECT_URI
            )
        )
        asyncio.run(_store_tokens(tokens))
    except Exception as exc:
        print(f"\n✗ No pude canjear el código por un token: {exc}")
        return 1

    print("\n✓ Listo. Ya puedes pedirle a Emma que publique en X.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
