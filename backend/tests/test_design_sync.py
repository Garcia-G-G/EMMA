"""Step 40.1: one design system, one route table, one mail template.

design/ is the source; backend/static/ and the landing repo (_landing/, checked out
inside this one in development) carry byte-identical copies. The landing checks
are skipped where _landing/ is absent (CI, a fresh clone).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend import mail, pages

ROOT = Path(__file__).resolve().parents[2]
DESIGN = ROOT / "design"
STATIC = ROOT / "backend" / "static"
LANDING = ROOT / "_landing"
needs_landing = pytest.mark.skipif(not LANDING.is_dir(), reason="_landing/ not checked out")

MIRRORED = sorted(
    p.relative_to(DESIGN) for p in DESIGN.rglob("*") if p.is_file() and p.name not in ("preview.html", ".DS_Store")
)
# A hex colour in CSS/JS. SVG paint attributes and in-page anchors are stripped first.
_HEX = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_ALLOWED = re.compile(r'(?:fill|stroke|stop-color|flood-color|lighting-color|color)="#[0-9a-fA-F]+"|href="#[\w-]*"')


def test_design_has_the_shared_files() -> None:
    names = {str(p) for p in MIRRORED}
    assert {"tokens.css", "components.css", "nav.js", "atom.svg", "email/atom-v1.png"} <= names


@pytest.mark.parametrize("rel", MIRRORED, ids=str)
def test_backend_copy_is_identical(rel: Path) -> None:
    assert (STATIC / rel).read_bytes() == (DESIGN / rel).read_bytes(), "run scripts/sync_design.sh"


@needs_landing
@pytest.mark.parametrize("rel", MIRRORED, ids=str)
def test_landing_copy_is_identical(rel: Path) -> None:
    assert (LANDING / rel).read_bytes() == (DESIGN / rel).read_bytes(), "run scripts/sync_design.sh"


def test_nav_routes_match_the_backend() -> None:
    js = (DESIGN / "nav.js").read_text()
    nav = {k: {"es": es, "en": en} for k, es, en in re.findall(r"(\w+): \{ es: '([^']+)', en: '([^']+)' \}", js)}
    assert nav.pop("home") == {"es": "/", "en": "/en/"}
    assert all(pages.ROUTES[k] == v for k, v in nav.items()), nav
    assert set(nav) <= set(pages.ROUTES)


@needs_landing
def test_landing_nginx_proxies_every_page() -> None:
    conf = (LANDING / "nginx.conf").read_text()
    m = re.search(r"location ~ \^/\((?P<es>[\w|]+)\)\$", conf)
    m_en = re.search(r"location ~ \^/en/\((?P<en>[\w|]+)\)\$", conf)
    assert m and m_en, "nginx.conf: page proxy locations not found"
    for paths in pages.ROUTES.values():
        assert paths["es"].lstrip("/") in m["es"].split("|")
        assert paths["en"].removeprefix("/en/") in m_en["en"].split("|")


@pytest.mark.parametrize("key", sorted(pages.ROUTES))
@pytest.mark.parametrize("lang", ["es", "en"])
def test_every_page_renders(key: str, lang: str) -> None:
    html = pages.render(key, lang)
    assert "{{" not in html and f'<html lang="{lang}">' in html


def _page_files() -> list[Path]:
    files = [*sorted((STATIC / "pages").glob("*.html")), STATIC / "admin.html"]
    if LANDING.is_dir():
        files += [LANDING / "index.html", LANDING / "404.html", LANDING / "landing.css"]
    return [f for f in files if f.exists()]


@pytest.mark.parametrize("path", _page_files(), ids=lambda p: p.name)
def test_pages_use_tokens_not_hex(path: Path) -> None:
    text = _ALLOWED.sub("", path.read_text(encoding="utf-8"))
    assert not _HEX.findall(text), f"{path.name}: use a token from tokens.css"


@needs_landing
def test_worker_mail_is_the_backend_template() -> None:
    ts = (LANDING / "worker" / "src" / "email.ts").read_text(encoding="utf-8")
    assert ts == mail.worker_ts(), "run: python -m backend.mail --worker-ts > _landing/worker/src/email.ts"


def test_mail_has_text_and_the_versioned_atom() -> None:
    for m in (mail.reset_message("https://x.test/r"), mail.welcome_message(), mail.waitlist_message()):
        assert m["text"].strip() and m["subject"]
        assert 'src="https://theemmafamily.com/email/atom-v1.png" width="48" height="48" alt="Emma"' in m["html"]
