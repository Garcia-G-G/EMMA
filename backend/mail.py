"""Transactional mail (password reset, welcome) through Resend.

One sender (``noreply@theemmafamily.com``) on the same Resend account as the
landing worker. The key is env ``RESEND_API_KEY`` on the server
(``/root/emma.env``), read by ``backend.config.Settings.RESEND_API_KEY``.

Check delivery without registering:

    python -m backend.mail --test <addr>
"""

from __future__ import annotations

import argparse
import asyncio
import html as _html
import json

import httpx
import structlog

from backend.config import settings

log = structlog.get_logger()

RESEND_URL = "https://api.resend.com/emails"
FROM = "Emma <noreply@theemmafamily.com>"
SITE = "https://theemmafamily.com"
# Versioned: a changed mark gets a new path, so mail already sent keeps its image.
ATOM_PNG = f"{SITE}/email/atom-v1.png"


def _layout(title: str, paragraphs: list[str], link: str = "") -> str:
    """Dark-amber body shared by every message; inline styles only (mail clients)."""
    body = "".join(
        "<tr><td style=\"font-family:-apple-system,'Helvetica Neue',sans-serif;font-weight:300;"
        f'font-size:16px;line-height:1.6;color:#ffd28a;padding-bottom:16px;">{_html.escape(p)}</td></tr>'
        for p in paragraphs
    )
    if link:
        body += (
            '<tr><td style="padding-bottom:24px;"><a href="' + _html.escape(link, quote=True) + '" '
            "style=\"font-family:'JetBrains Mono',ui-monospace,monospace;font-size:13px;"
            f'color:#ff7a1a;word-break:break-all;">{_html.escape(link)}</a></td></tr>'
        )
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:0;background:#0a0400;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#0a0400;padding:48px 16px;"><tr><td align="center">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;">'
        # The atom as a 2x PNG with the dark background baked in (Gmail and Outlook
        # drop inline SVG; a transparent PNG gets inverted by Outlook's dark mode).
        '<tr><td style="padding-bottom:28px;"><table role="presentation" cellpadding="0" cellspacing="0">'
        f'<tr><td style="padding-right:12px;"><img src="{ATOM_PNG}" width="48" height="48" alt="Emma" '
        'style="display:block;border:0;outline:none;"></td>'
        "<td style=\"font-family:'Instrument Serif',ui-serif,serif;font-size:26px;line-height:1;"
        'color:#fff6dc;">Emma</td></tr></table></td></tr>'
        "<tr><td style=\"font-family:'Instrument Serif',ui-serif,serif;font-size:34px;line-height:1.1;"
        f'color:#fff6dc;padding-bottom:20px;">{_html.escape(title)}</td></tr>'
        f"{body}"
        '<tr><td style="border-top:1px solid rgba(255,122,26,0.30);padding-top:20px;'
        "font-family:'JetBrains Mono',ui-monospace,monospace;font-size:10.5px;letter-spacing:0.22em;"
        f'text-transform:uppercase;"><a href="{SITE}" style="color:#ff7a1a;text-decoration:none;">'
        "theemmafamily.com</a></td></tr></table></td></tr></table></body></html>"
    )


def _text(title: str, paragraphs: list[str], link: str = "") -> str:
    return "\n\n".join([title, *paragraphs, *([link] if link else []), SITE]) + "\n"


def _message(subject: str, title: str, paragraphs: list[str], link: str = "") -> dict[str, str]:
    return {
        "subject": subject,
        "html": _layout(title, paragraphs, link),
        "text": _text(title, paragraphs, link),
    }


def reset_message(link: str) -> dict[str, str]:
    return _message(
        "Restablece tu contraseña de Emma · Reset your Emma password",
        "Restablece tu contraseña",
        [
            "Abre este enlace para elegir una contraseña nueva. Vale una hora.",
            "Open this link to choose a new password. It is valid for one hour.",
            "Si no lo pediste, ignora este correo. · If you didn't ask for this, ignore this email.",
        ],
        link,
    )


def welcome_message() -> dict[str, str]:
    return _message(
        "Tu cuenta de Emma · Your Emma account",
        "Tu cuenta está lista",
        [
            "Ya puedes entrar con tu correo. · You can now sign in with your email.",
            "Si no creaste esta cuenta, ignora este correo. · "
            "If you didn't create this account, ignore this email.",
        ],
        f"{SITE}/entrar",
    )


def waitlist_message() -> dict[str, str]:
    """The landing worker's confirmation (sent from hello@ by _landing/worker)."""
    return _message(
        "Anotado · You're on the list",
        "Anotado",
        [
            "Te escribiremos cuando haya novedades de Emma.",
            "We'll email you when there is news about Emma.",
            "Si no fuiste tú, ignora este correo. · If this wasn't you, ignore this email.",
        ],
    )


def worker_ts() -> str:
    """_landing/worker/src/email.ts: the worker sends the same template as the backend."""
    m = waitlist_message()
    return (
        "// Generated by `python -m backend.mail --worker-ts` from backend/mail.py. Do not edit.\n"
        f"export const CONFIRM_SUBJECT = {json.dumps(m['subject'], ensure_ascii=False)};\n"
        f"export const CONFIRM_TEXT = {json.dumps(m['text'], ensure_ascii=False)};\n"
        f"export const CONFIRM_HTML = {json.dumps(m['html'], ensure_ascii=False)};\n"
    )


def test_message() -> dict[str, str]:
    return _message(
        "Prueba de correo de Emma · Emma mail test",
        "Prueba",
        ["Si lees esto, el correo llega. · If you can read this, mail arrives."],
    )


async def send(to: str, message: dict[str, str]) -> tuple[int, str] | None:
    """Send one message; returns (status, Resend's body), or None if not sent. Never raises."""
    key = settings.RESEND_API_KEY
    if not key:
        log.info("mail_skipped", reason="no RESEND_API_KEY")
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(
                RESEND_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={"from": FROM, "to": [to], **message},
            )
    except httpx.HTTPError as exc:
        log.warning("mail_failed", error=type(exc).__name__)
        return None
    if r.status_code >= 300:
        log.warning("mail_rejected", status=r.status_code, body=r.text[:300])
    return r.status_code, r.text


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m backend.mail")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--test", metavar="ADDR", help="send one test message to ADDR")
    g.add_argument(
        "--worker-ts", action="store_true", help="print the landing worker's email module"
    )
    args = p.parse_args(argv)
    if args.worker_ts:
        print(worker_ts(), end="")
        return 0
    res = asyncio.run(send(args.test, test_message()))
    if res is None:
        print("not sent: RESEND_API_KEY unset or network error")
        return 1
    print(res[0], res[1])
    return 0 if res[0] < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
