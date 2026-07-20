"""GitHub repo search for Emma's voice flow.

Two tools:

- ``search_github(query, limit=5)`` returns up to 5 matching public
  repositories. Each match has ``name``, ``full_name``, ``url``,
  ``clone_url``, ``description``, ``stars``, ``language``.

- ``get_repo_url(query)`` is a convenience wrapper returning the single top
  match's ``clone_url`` (or the failure result). Used when Emma chains
  search → clone in one voice command.

A ``GITHUB_TOKEN`` env var is honored if present (raises the rate limit from
60/hr to 5000/hr). It is treated as a credential by the 15.6 Keychain migration.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import structlog

from config.settings import settings
from core import dictionary
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.github")

_API = "https://api.github.com/search/repositories"
_API_ROOT = "https://api.github.com"
_TIMEOUT = 8.0

# A GitHub login: 1-39 chars, alphanumeric or hyphen, can't start with a hyphen.
# Used by the zero-result retry to decide a query is a handle, not a repo name.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,38}$")


def _looks_like_handle(q: str) -> bool:
    """True if ``q`` is a single token that could be a GitHub username/handle."""
    return " " not in q and bool(_HANDLE_RE.match(q))


def _repo_match(it: dict[str, Any]) -> dict[str, Any]:
    """Normalize a GitHub repo object to Emma's match shape (shared by all paths)."""
    return {
        "name": it["name"],
        "full_name": it["full_name"],
        "url": it["html_url"],
        "clone_url": it["clone_url"],
        "description": (it.get("description") or "")[:160],
        "stars": it.get("stargazers_count", 0),
        "language": it.get("language") or "",
        "private": bool(it.get("private", False)),
    }


def _matches_result(matches: list[dict[str, Any]], header: str) -> ToolResult:
    """Build the standard search-shaped ToolResult from a list of matches."""
    summary = "\n".join(
        f"{i + 1}. {m['full_name']} ({m['stars']}★) — {m['description'] or 'sin descripción'}"
        for i, m in enumerate(matches)
    )
    return ToolResult(
        True,
        {"matches": matches, "top": matches[0]},
        f"{header}\n{summary}",
        False,
    )


# Scoping qualifiers that point at a named resource. If that resource does not
# exist (e.g. the model guessed a username), GitHub 422s the *entire* query
# instead of returning empty results. We strip these and retry as free text.
_SCOPE_RE = re.compile(r"\b(?:user|org|repo|repository):\S+", re.IGNORECASE)


def _strip_scope_qualifiers(q: str) -> str:
    """Drop user:/org:/repo: qualifiers, leaving the free-text remainder."""
    return re.sub(r"\s+", " ", _SCOPE_RE.sub(" ", q)).strip()


def _github_error_message(r: httpx.Response) -> str:
    """Pull GitHub's human-readable validation message out of an error body,
    falling back to a bare status code if the body isn't the expected shape."""
    try:
        body = r.json()
    except Exception:
        return f"error {r.status_code}"
    errs = body.get("errors")
    if isinstance(errs, list) and errs and errs[0].get("message"):
        return str(errs[0]["message"])
    return str(body.get("message") or f"error {r.status_code}")


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "emma/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = settings.GITHUB_TOKEN
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


@tool(returns_untrusted_content=True)
async def search_github(query: str, limit: int = 5) -> ToolResult:
    """Search GitHub public repositories by name or keyword.

    Use when Garcia says any of:
    - "Emma, busca el repo X en GitHub"
    - "Emma, búscame un repo de Y"
    - "Emma, ¿hay un proyecto open source de Z?"
    """
    q = (query or "").strip()
    if not q:
        return ToolResult(False, None, "Dime qué buscar.", False)
    base: dict[str, str | int] = {"per_page": max(1, min(limit, 10)), "sort": "stars"}

    def _rate_limited(r: httpx.Response) -> bool:
        return r.status_code == 403 and "rate limit" in r.text.lower()

    rate_result = ToolResult(
        False,
        None,
        "GitHub me limitó el ritmo. Espera unos minutos o agrega un GITHUB_TOKEN al .env.",
        False,
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
            r = await cli.get(_API, headers=_headers(), params={"q": q, **base})
            if _rate_limited(r):
                return rate_result
            # A 422 here usually means a user:/org:/repo: qualifier named a
            # resource that doesn't exist. Strip those and retry as free text.
            if r.status_code == 422:
                stripped = _strip_scope_qualifiers(q)
                if stripped and stripped != q:
                    log.info("github_search_retry_stripped", original=q, stripped=stripped)
                    q = stripped
                    r = await cli.get(_API, headers=_headers(), params={"q": q, **base})
                    if _rate_limited(r):
                        return rate_result

            if not (200 <= r.status_code < 300):
                msg = _github_error_message(r)
                log.error("github_search_failed", status=r.status_code, error=msg)
                return ToolResult(False, None, f"No pude buscar en GitHub: {msg}", False)

            items = r.json().get("items", [])
            if items:
                matches = [_repo_match(it) for it in items[:limit]]
                return _matches_result(matches, f"Encontré {len(matches)}:")

            # B14: zero results on a single-token query that looks like a handle —
            # it's probably a username, not a repo name. Try the user's repos.
            if _looks_like_handle(q):
                ur = await cli.get(f"{_API_ROOT}/users/{q}", headers=_headers())
                if ur.status_code == 200:
                    rr = await cli.get(
                        f"{_API_ROOT}/users/{q}/repos",
                        headers=_headers(),
                        params={
                            "sort": "stars",
                            "per_page": max(1, min(limit, 10)),
                            "type": "owner",
                        },
                    )
                    if 200 <= rr.status_code < 300:
                        repos = rr.json()
                        if isinstance(repos, list) and repos:
                            matches = [_repo_match(it) for it in repos[:limit]]
                            log.info("github_search_user_fallback", handle=q, found=len(matches))
                            return _matches_result(
                                matches, f"Encontré {len(matches)} repos del usuario {q}:"
                            )
                # 21-B25: before giving up, check whether the query is a
                # mistranscription of Garcia's OWN username (the most common
                # case: "gilbergaciata" → his handle). One mechanism — the
                # transversal suggest_similar — never bespoke fuzzy logic.
                from core import dictionary
                from tools.disambiguation import suggest_similar

                own = dictionary.user_profile().get("github_username", "")
                if own and suggest_similar(q, [own], threshold=0.6):
                    return ToolResult(
                        True,
                        {"matches": [], "suggestions": [own]},
                        f"No encontré '{q}', pero se parece a tu usuario '{own}'. "
                        "¿Te enseño tus repos?",
                        requires_confirmation=True,
                    )
                return ToolResult(
                    True,
                    {"matches": []},
                    f"No encontré repos llamados '{q}'. Tampoco existe un usuario con ese "
                    "nombre exacto en GitHub. ¿Quizás quieres pedirme 'mis repos'?",
                    False,
                )
    except Exception as exc:
        log.error("github_search_failed", error=str(exc))
        return ToolResult(False, None, f"No pude buscar en GitHub: {exc}", False)

    return ToolResult(True, {"matches": []}, f"No encontré repos para '{q}'.", False)


@tool(returns_untrusted_content=True)
async def my_repos(sort: str = "updated", limit: int = 5, visibility: str = "all") -> ToolResult:
    """Lista los repos de Garcia (los suyos, no search). Para 'mis repos',
    'los repos que tengo', 'el repo que hice de X'.

    Usa /user/repos (autenticado: públicos + privados) o /users/<usuario>/repos
    (público) según haya GITHUB_TOKEN. `sort` ∈ updated, created, pushed,
    full_name; `visibility` ∈ all, public, private."""
    username = dictionary.user_profile().get("github_username", "").strip()
    if not username:
        return ToolResult(
            False,
            None,
            "No tengo tu usuario de GitHub. Dime cuál es y lo guardo (remember_user_profile).",
            False,
        )
    per_page = max(1, min(limit, 10))
    vis = visibility if visibility in ("all", "public", "private") else "all"
    token = settings.GITHUB_TOKEN
    if token:
        url = f"{_API_ROOT}/user/repos"
        params: dict[str, str | int] = {
            "sort": sort,
            "per_page": per_page,
            "visibility": vis,
            "affiliation": "owner",  # Garcia's own repos, not orgs he collaborates on
        }
    else:
        url = f"{_API_ROOT}/users/{username}/repos"
        params = {"sort": sort, "per_page": per_page, "type": "owner"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as cli:
            r = await cli.get(url, headers=_headers(), params=params)
            if r.status_code == 403 and "rate limit" in r.text.lower():
                return ToolResult(
                    False,
                    None,
                    "GitHub me limitó el ritmo. Espera unos minutos o agrega un GITHUB_TOKEN.",
                    False,
                )
    except Exception as exc:
        log.error("my_repos_failed", error=str(exc))
        return ToolResult(False, None, f"No pude leer tus repos: {exc}", False)

    if not (200 <= r.status_code < 300):
        msg = _github_error_message(r)
        log.error("my_repos_failed", status=r.status_code, error=msg)
        return ToolResult(False, None, f"No pude leer tus repos: {msg}", False)

    repos = r.json()
    if not isinstance(repos, list) or not repos:
        return ToolResult(True, {"matches": []}, "No encontré repos en tu cuenta.", False)
    matches = [_repo_match(it) for it in repos[:limit]]
    return _matches_result(matches, f"Tienes {len(matches)} repos:")


@tool(returns_untrusted_content=True)
async def get_repo_url(query: str) -> ToolResult:
    """Resolve a repo query to its top clone URL. Used when Garcia chains
    'busca X y clónalo' in one breath."""
    res = await search_github(query, limit=1)
    if not res.success or not (res.data and res.data.get("matches")):
        return res
    top = res.data["matches"][0]
    return ToolResult(
        True,
        {"clone_url": top["clone_url"], "full_name": top["full_name"]},
        f"{top['full_name']}",
        False,
    )
