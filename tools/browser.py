"""Playwright-driven browser tools.

Phase 02 ships lower-level helpers and one case-specific
``browser_do(intent)`` that scripts the Amazon "find product and add to
cart" flow with a confirmation gate.

# TODO(future-phase): replace the scripted browser_do with a general
#   LLM-driven agent. The agent should take a free-form intent, drive the
#   browser turn-by-turn using screenshots + DOM context with GPT-4o,
#   structured action grammar, retries, and a hard step cap (15). Until
#   then, any non-Amazon intent surfaces a clean "not yet available"
#   error so the LLM falls back to lower-level tools or asks the user.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import structlog

from config.settings import settings
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.browser")

_PROFILE_DIR = settings.EMMA_HOME / "playwright-profile"

_playwright: Any = None
_context: Any = None
_lock = asyncio.Lock()


async def _ensure_context() -> Any:
    """Lazy-launch the persistent Chromium context. Reused across calls."""
    global _playwright, _context
    async with _lock:
        if _context is not None:
            return _context
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright not installed. Run `uv run playwright install chromium`."
            ) from exc
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _playwright = await async_playwright().start()
        _context = await _playwright.chromium.launch_persistent_context(
            user_data_dir=str(_PROFILE_DIR),
            headless=settings.BROWSER_HEADLESS,
            viewport={"width": 1280, "height": 800},
        )
        return _context


async def _active_page() -> Any:
    ctx = await _ensure_context()
    if ctx.pages:
        return ctx.pages[-1]
    return await ctx.new_page()


@tool()
async def browser_navigate(url: str) -> ToolResult:
    """Open `url` in Emma's persistent Chromium window."""
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    try:
        page = await _active_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    except Exception as exc:
        return ToolResult(False, None, f"No pude navegar a {url}: {exc}", False)
    return ToolResult(True, {"url": url}, f"Abrí {url}.", False)


@tool()
async def browser_click(selector: str) -> ToolResult:
    """Click an element. `selector` accepts CSS, ``text=...``, or ``role=...``."""
    try:
        page = await _active_page()
        await page.locator(selector).first.click(timeout=10000)
    except Exception as exc:
        return ToolResult(False, None, f"No pude hacer click en '{selector}': {exc}", False)
    return ToolResult(True, {"selector": selector}, "Listo.", False)


@tool()
async def browser_type(selector: str, text: str) -> ToolResult:
    """Type `text` into a focused field matching `selector`."""
    try:
        page = await _active_page()
        await page.locator(selector).first.fill(text, timeout=10000)
    except Exception as exc:
        return ToolResult(False, None, f"No pude escribir en '{selector}': {exc}", False)
    return ToolResult(True, {"selector": selector, "text": text}, "Escrito.", False)


@tool()
async def browser_screenshot() -> ToolResult:
    """Capture the current page. Returns the PNG path."""
    try:
        page = await _active_page()
        path = settings.EMMA_HOME / "last_screenshot.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=False)
    except Exception as exc:
        return ToolResult(False, None, f"Screenshot falló: {exc}", False)
    return ToolResult(True, {"path": str(path)}, "Captura guardada.", False)


# ----- Scripted Amazon flow -------------------------------------------------

_STOPWORDS_RE = re.compile(
    r"\b(busca|buscar|compra|comprar|agrega|agregar|al carrito|en amazon|"
    r"search|buy|add|to (the )?cart|on amazon|unos|unas|un|una)\b",
    re.IGNORECASE,
)


def _extract_product(intent: str) -> str:
    cleaned = _STOPWORDS_RE.sub(" ", intent)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.")
    return cleaned


@tool(destructive=True)
async def browser_do(intent: str, confirmed: bool = False) -> ToolResult:
    """Run a multi-step browser task described in natural language.

    Currently only the Amazon "find product and add to cart" flow is
    scripted - non-Amazon intents return a clean error so the LLM falls
    back to ``browser_navigate`` and friends.
    """
    if not re.search(r"amazon|carrito|cart", intent, re.IGNORECASE):
        return ToolResult(
            False,
            None,
            (
                "El agente general del navegador aún no está disponible. "
                "Por ahora solo puedo agregar productos al carrito de Amazon."
            ),
            False,
        )

    product = _extract_product(intent)
    if not product:
        return ToolResult(False, None, "No pude identificar el producto.", False)

    page = await _active_page()

    if not confirmed:
        # Phase 1: navigate to Amazon, search, click first result, await confirmation.
        try:
            await page.goto("https://www.amazon.com", wait_until="domcontentloaded", timeout=20000)
            await page.locator('input[name="field-keywords"]').first.fill(product)
            await page.locator('input[name="field-keywords"]').first.press("Enter")
            await page.wait_for_selector(
                'div[data-component-type="s-search-result"]', timeout=15000
            )
            await page.locator(
                'div[data-component-type="s-search-result"] h2 a'
            ).first.click()
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            title = (await page.title()).strip()
        except Exception as exc:
            log.error("amazon_search_failed", error=str(exc))
            return ToolResult(False, None, f"No pude completar la búsqueda en Amazon: {exc}", False)
        short_title = title.split(":")[0][:80] if title else product
        return ToolResult(
            True,
            {"product": product, "page_title": title},
            f"Encontré '{short_title}'. ¿Lo agrego al carrito?",
            requires_confirmation=True,
        )

    # Phase 2: user confirmed. Click Add-to-Cart on the current product page.
    try:
        add_btn = page.locator("#add-to-cart-button")
        await add_btn.first.click(timeout=10000)
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception as exc:
        log.error("amazon_add_to_cart_failed", error=str(exc))
        return ToolResult(False, None, f"No pude agregarlo al carrito: {exc}", False)
    return ToolResult(
        True,
        {"product": product},
        f"Listo. Agregué {product} al carrito.",
        False,
    )


async def shutdown_browser() -> None:
    """Close the persistent context on graceful shutdown."""
    global _context, _playwright
    if _context is not None:
        try:
            await _context.close()
        except Exception:
            pass
        _context = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None
