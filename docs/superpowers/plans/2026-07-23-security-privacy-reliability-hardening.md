# Emma Security, Privacy, and Reliability Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove identity leaks and rejected visuals, fix confirmed memory and async lifecycle defects, and strengthen backend authentication without destabilizing Emma's voice runtime.

**Architecture:** Preserve existing module boundaries and apply narrow changes at the established seams: repository privacy invariants, memory query helpers, WebSocket pump ownership, request-scoped HTTP clients, password primitives, and backend request middleware. Each block receives focused regression tests before the full repository verification and its own Git commit.

**Tech Stack:** Python 3.12, asyncio, SQLite/sqlite-vec, FastAPI/Starlette, HTTPX, pytest, inline SVG/CSS, Ruff, mypy.

## Global Constraints

- Public copy must never identify the maker or claim a geographic origin.
- Code comments, prompts, fixtures, examples, and generated assets must use generic user language rather than a real identity or location.
- The atom is Emma's visual mark. The rejected Three.js wireframe and plain sphere directions must not remain in shipping surfaces.
- Secret-tier values must never enter SQLite memory, logs, prompts, fixtures, or committed files.
- No new macOS permissions are introduced.
- No new long-running synchronous tool behavior is introduced.
- Preserve the untracked `AGENTS.md` and unrelated user changes.
- Backend dependencies remain isolated from the daemon's `pyproject.toml`.
- Run focused tests before every commit and push.

---

### Task 1: Enforce Repository Identity Privacy

**Files:**
- Create: `tests/test_public_copy_invariants.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `SECURITY.md`
- Modify: `ERRORS-TO-FIX.md`
- Modify: `actions/*.py`, `backend/*.py`, `config/*.py`, `core/*.py`, `data/README.md`
- Modify: `emma/*.py`, `installer/*`, `memory/*.py`, `scripts/*.py`, `tools/*.py`
- Modify: `tests/**/*.py`, `tests/acceptance/scenarios.yaml`, `tests/acceptance/audio_cache/manifest.json`
- Regenerate: `self/capabilities.md`

**Interfaces:**
- Consumes: repository text files returned by `rg --files`
- Produces: a privacy invariant test that fails when shipping implementation or public-copy files contain forbidden identity/location phrases

- [ ] **Step 1: Write the failing repository invariant**

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    "actions", "backend", "config", "core", "data", "emma", "installer",
    "memory", "scripts", "self", "tools",
)
FORBIDDEN = ("<maker-name>", "<maker-city>", "made by <maker-name>")


def test_shipping_surfaces_do_not_assume_maker_identity() -> None:
    violations: list[str] = []
    for root_name in SCAN_ROOTS:
        for path in (ROOT / root_name).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md", ".html", ".toml", ".xml", ".sh"}:
                text = path.read_text(encoding="utf-8", errors="ignore")
                for phrase in FORBIDDEN:
                    if phrase.casefold() in text.casefold():
                        violations.append(f"{path.relative_to(ROOT)}: {phrase}")
    assert violations == []
```

Derive the forbidden phrases from a small test-only tuple assembled from neutral
fragments, so the complete private identity/location strings are not repeated in
source. Allowlist only exact legacy compatibility identifiers that the installer
must remove during migration; do not allowlist prose, prompts, metadata, examples,
or generated documentation.

- [ ] **Step 2: Run the invariant and confirm it reports current leaks**

Run: `.venv/bin/python -m pytest tests/test_public_copy_invariants.py -q`

Expected: FAIL listing implementation, installer, generated documentation, and public-copy files.

- [ ] **Step 3: Replace identity assumptions mechanically, then review every changed sentence**

Apply these semantic mappings:

```text
maker's name / possessive -> "the user" / "the user's"
named home city or city-specific timezone -> generic example or "UTC"
"maker's Mac" -> "the user's Mac"
"maker's voice" -> "the user's voice"
package author metadata -> remove the optional `authors` field
Windows manufacturer/registry identity -> "Emma" / "Software\\Emma"
sample spoken identity -> "hola, soy Alex"
```

Do not alter the repository guidance file containing the enforcement rules.
Preserve only the exact legacy LaunchAgent and Keychain identifiers required for
upgrade/uninstall compatibility, with generic comments explaining that they are
legacy identifiers rather than naming a person.
Update tests and acceptance fixtures to fictional names and locations such as
`Alex`, `Sam Doe`, `San José`, or `UTC`, depending on the behavior under test.

- [ ] **Step 4: Regenerate capabilities through the supported generator**

Run:

```bash
.venv/bin/python -c "from tools.registry import all_tools; from tools.self_tool import regenerate_capabilities_md; all_tools(); regenerate_capabilities_md()"
```

Expected: `self/capabilities.md` is regenerated from clean tool descriptions and contains no forbidden identity/location text.

- [ ] **Step 5: Run focused privacy and identity tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_public_copy_invariants.py tests/test_identity.py tests/test_conversation.py tests/test_personality.py tests/test_x_privacy_invariant.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit and push the privacy block**

```bash
git add pyproject.toml README.md SECURITY.md ERRORS-TO-FIX.md actions backend config core data emma installer memory scripts self tools tests
git commit -m "privacy: remove personal identity from shipping surfaces"
git push origin main
```

Expected: push succeeds without staging `AGENTS.md`.

---

### Task 2: Replace the Rejected Wireframe With the Atom

**Files:**
- Modify: `dashboard/visualizer.html`
- Modify: `README.md`
- Test: `tests/test_visualizer.py`
- Test: `tests/test_public_copy_invariants.py`

**Interfaces:**
- Consumes: existing dashboard WebSocket event/state names
- Produces: the same event-driven visualizer surface rendered with inline SVG/CSS atom elements and no Three.js/WebGL dependency

- [ ] **Step 1: Add failing markup and dependency assertions**

```python
def test_visualizer_uses_atom_identity() -> None:
    html = VISUALIZER.read_text(encoding="utf-8")
    assert 'class="emma-atom"' in html
    assert 'class="atom-nucleus"' in html
    assert html.count('class="atom-orbit') >= 3
    assert "THREE." not in html
    assert "three.min.js" not in html
    assert "WebGLRenderer" not in html
```

- [ ] **Step 2: Run the visualizer tests and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_visualizer.py tests/test_public_copy_invariants.py -q`

Expected: FAIL because the current page uses Three.js wireframes.

- [ ] **Step 3: Implement the inline atom while preserving state hooks**

Replace the WebGL scene with:

```html
<svg class="emma-atom" viewBox="0 0 320 320" aria-label="Emma">
  <g class="atom-orbits">
    <ellipse class="atom-orbit orbit-a" cx="160" cy="160" rx="118" ry="42"/>
    <ellipse class="atom-orbit orbit-b" cx="160" cy="160" rx="118" ry="42"
             transform="rotate(60 160 160)"/>
    <ellipse class="atom-orbit orbit-c" cx="160" cy="160" rx="118" ry="42"
             transform="rotate(120 160 160)"/>
  </g>
  <circle class="atom-nucleus" cx="160" cy="160" r="15"/>
  <circle class="atom-electron electron-a" cx="278" cy="160" r="5"/>
  <circle class="atom-electron electron-b" cx="101" cy="58" r="5"/>
  <circle class="atom-electron electron-c" cx="101" cy="262" r="5"/>
</svg>
```

Map existing listening/thinking/speaking/error events to CSS custom properties,
classes, pulse speed, glow intensity, and orbit speed. Retain reduced-motion
support and all existing WebSocket connection behavior.

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/python -m pytest tests/test_visualizer.py tests/test_public_copy_invariants.py -q`

Expected: PASS.

- [ ] **Step 5: Commit and push the atom block**

```bash
git add dashboard/visualizer.html README.md tests/test_visualizer.py tests/test_public_copy_invariants.py
git commit -m "design: make the atom Emma's visual identity"
git push origin main
```

---

### Task 3: Fix Memory Supersession Review

**Files:**
- Modify: `memory/long_term.py:613`
- Modify: `tests/test_supersede.py`

**Interfaces:**
- Consumes: `recent_supersessions(limit: int = 20) -> list[dict[str, Any]]`
- Produces: ordered replacement records with `new_id`, `new_content`, `old_id`, `old_content`, and `at`

- [ ] **Step 1: Add a failing regression test**

```python
def test_recent_supersessions_returns_replacement_pair(self, tmp_mem):
    old = _seed("The user prefers VSCode")
    new = lt._supersede_insert_sync(
        old, "The user prefers Zed", "preference", 0.9, "test",
        [0.2] * lt._EMBED_DIMS,
    )
    rows = asyncio.run(lt.recent_supersessions())
    assert rows == [{
        "new_id": new,
        "new_content": "The user prefers Zed",
        "old_id": old,
        "old_content": "The user prefers VSCode",
        "at": rows[0]["at"],
    }]
```

- [ ] **Step 2: Verify the SQL failure**

Run: `.venv/bin/python -m pytest tests/test_supersede.py::TestReadFilters::test_recent_supersessions_returns_replacement_pair -q`

Expected: FAIL with SQLite syntax error near `FROM`.

- [ ] **Step 3: Remove the duplicated SQL clause**

```python
rows = conn.execute(
    "SELECT n.id AS new_id, n.content AS new_content, "
    "o.id AS old_id, o.content AS old_content, o.superseded_at AS at "
    "FROM facts n JOIN facts o ON n.supersedes = o.id "
    "WHERE n.supersedes IS NOT NULL ORDER BY o.superseded_at DESC LIMIT ?",
    (limit,),
).fetchall()
```

- [ ] **Step 4: Run the supersession suite**

Run: `.venv/bin/python -m pytest tests/test_supersede.py tests/test_memory_review.py -q`

Expected: PASS.

---

### Task 4: Make Async Resource Cleanup Deterministic

**Files:**
- Modify: `backend/realtime_proxy.py`
- Modify: `backend/demo_session.py`
- Modify: `backend/auth_local.py`
- Modify: `backend/app.py`
- Test: `backend/tests/test_realtime_proxy.py`
- Test: `backend/tests/test_demo_session.py`
- Test: `backend/tests/test_auth.py`

**Interfaces:**
- Consumes: existing WebSocket proxy routes and `_send_reset_email(email, link)`
- Produces: `_cancel_and_await(tasks: set[asyncio.Task[Any]]) -> None` or an equivalent private helper used by all proxy paths

- [ ] **Step 1: Add failing cleanup tests**

Create tasks whose `finally` blocks set an `asyncio.Event`, invoke the cleanup
helper, and assert every event is set and every task is done. Mock
`httpx.AsyncClient` as an async context manager and assert `__aexit__` runs after
the reset-email request.

- [ ] **Step 2: Run focused tests and confirm failure**

Create the isolated backend environment once if it does not exist:

```bash
uv venv backend/.venv --python 3.12
uv pip install --python backend/.venv/bin/python -r backend/requirements.txt pytest pytest-asyncio pip-audit
```

Then run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_realtime_proxy.py backend/tests/test_demo_session.py backend/tests/test_auth.py -q
```

Expected: FAIL because the helper/context-managed reset client does not exist.

- [ ] **Step 3: Implement shared cancellation cleanup**

```python
async def _cancel_and_await(tasks: set[asyncio.Task[Any]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
```

Use it in the demo Realtime proxy and live demo bridge after
`asyncio.wait(..., return_when=asyncio.FIRST_COMPLETED)`. Preserve the device
path's warning-ticker cleanup and metering `finally` block.

- [ ] **Step 4: Close the reset-email HTTP client**

```python
async with httpx.AsyncClient(timeout=8.0) as client:
    await client.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {key}"},
        json=payload,
    )
```

Keep reset-email delivery best-effort and ensure logs never contain the reset
token or link.

- [ ] **Step 5: Manage the module-level OpenAI HTTP client at application shutdown**

Expose an async close function in `backend/openai_proxy.py` and call it from the
FastAPI lifespan cleanup in `backend/app.py`. Preserve database initialization at
startup and use `TestClient` as a context manager where lifecycle assertions are
needed.

- [ ] **Step 6: Run focused backend tests**

Run the same command from Step 2.

Expected: PASS with no pending-task or unclosed-client warnings.

- [ ] **Step 7: Commit and push correctness/reliability**

```bash
git add memory/long_term.py tests/test_supersede.py backend/realtime_proxy.py backend/demo_session.py backend/auth_local.py backend/openai_proxy.py backend/app.py backend/tests
git commit -m "fix: harden memory and async resource lifecycles"
git push origin main
```

---

### Task 5: Strengthen Password Storage and Upgrade Legacy Hashes

**Files:**
- Modify: `backend/passwords.py`
- Modify: `backend/auth_local.py`
- Modify: `backend/account_routes.py`
- Modify: `backend/db.py`
- Modify: `backend/tests/test_auth.py`
- Modify: `backend/tests/test_account.py`

**Interfaces:**
- Produces: `password_needs_rehash(stored: str) -> bool`
- Produces: a single maximum password length constant used by validation and verification
- Consumes: existing `db.set_password(user_id: int, password_hash: str) -> None`

- [ ] **Step 1: Add failing password-strength and migration tests**

```python
def test_new_hash_uses_current_work_factor():
    assert hash_password("correcthorse9").split("$")[1] == "600000"


def test_legacy_hash_needs_rehash():
    legacy = _legacy_hash("correcthorse9", iterations=240_000)
    assert verify_password("correcthorse9", legacy)
    assert password_needs_rehash(legacy)


def test_login_upgrades_legacy_hash(client):
    legacy = _legacy_hash("correcthorse9", iterations=240_000)
    user = db.create_local_user("legacy@example.test", legacy)
    response = client.post("/api/auth/login", json={
        "email": "legacy@example.test", "password": "correcthorse9",
    })
    assert response.status_code == 200
    upgraded = db.get_user(user["id"])["password_hash"]
    assert upgraded.split("$")[1] == "600000"
```

Add a monkeypatched `hashlib.pbkdf2_hmac` assertion proving an overlong password
returns a validation error without calling PBKDF2.

- [ ] **Step 2: Run password tests and confirm failure**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_auth.py backend/tests/test_account.py -q`

Expected: FAIL on work factor, migration helper, and length bound.

- [ ] **Step 3: Implement current hashing and bounded verification**

```python
_ITERS = 600_000
_MAX_PASSWORD_CHARS = 1024


def password_needs_rehash(stored: str) -> bool:
    try:
        algo, iters_s, _salt_hex, _hash_hex = stored.split("$")
        return algo != _ALGO or int(iters_s) < _ITERS
    except (ValueError, AttributeError):
        return True
```

Reject passwords longer than `_MAX_PASSWORD_CHARS` in `password_problem`, and
make `verify_password` return `False` before PBKDF2 for overlong input or invalid
iteration counts. After successful login, call `db.set_password` only when
`password_needs_rehash` is true.

- [ ] **Step 4: Run password/account tests**

Run the same command from Step 2.

Expected: PASS.

---

### Task 6: Protect Cookie-Authenticated Mutations

**Files:**
- Modify: `backend/auth.py`
- Modify: `backend/app.py`
- Create: `backend/request_security.py`
- Modify: `backend/tests/test_auth.py`
- Modify: `backend/tests/test_security_fixes.py`
- Modify: affected backend HTML/JavaScript only if requests need an explicit Origin-compatible change

**Interfaces:**
- Produces: `validate_browser_request(request: Request) -> None`
- Produces: environment-aware session cookie names for local HTTP and production HTTPS
- Consumes: `settings.PUBLIC_URL`, bearer `Authorization` headers, and the existing signed session serializer

- [ ] **Step 1: Add failing request-integrity tests**

```python
def test_cross_origin_cookie_mutation_is_rejected(client):
    _register(client)
    response = client.post(
        "/api/me/email",
        headers={"Origin": "https://attacker.example"},
        json={"email": "new@example.test"},
    )
    assert response.status_code == 403


def test_same_origin_cookie_mutation_succeeds(client):
    _register(client)
    response = client.post(
        "/api/me/email",
        headers={"Origin": "http://localhost"},
        json={"email": "new@example.test"},
    )
    assert response.status_code == 200
```

Also assert production `Set-Cookie` contains `__Host-`, `Secure`, `HttpOnly`,
`Path=/`, and the selected explicit SameSite policy, while local HTTP tests
remain compatible.

- [ ] **Step 2: Verify current behavior fails the security expectations**

Run: `backend/.venv/bin/python -m pytest backend/tests/test_auth.py backend/tests/test_security_fixes.py -q`

Expected: FAIL because cross-origin mutations currently succeed and production
cookie migration is absent.

- [ ] **Step 3: Implement origin validation middleware**

For unsafe methods, when a valid session cookie is present:

```python
origin = request.headers.get("origin")
expected = normalized_origin(settings.PUBLIC_URL)
if origin is None or normalized_origin(origin) != expected:
    return JSONResponse({"detail": "Origen no permitido."}, status_code=403)
```

Exclude Stripe webhooks, OAuth callbacks, anonymous registration/login/reset,
and bearer-only device routes based on explicit route/auth semantics. Do not
trust `Host` or forwarded headers as the expected origin; use `PUBLIC_URL`.

- [ ] **Step 4: Implement cookie migration**

Use `__Host-emma_session` on HTTPS and `emma_session` on local HTTP. During
authentication reads, accept the current cookie first and the legacy cookie
second. When setting or clearing a session, expire the legacy name as well.
Add `Cache-Control: no-store` to responses that set or clear authentication.

- [ ] **Step 5: Run the complete backend auth/security surface**

Run:

```bash
backend/.venv/bin/python -m pytest backend/tests/test_auth.py backend/tests/test_account.py backend/tests/test_account_surface.py backend/tests/test_security_fixes.py backend/tests/test_stripe.py backend/tests/test_device_pairing.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit and push authentication hardening**

```bash
git add backend/passwords.py backend/auth.py backend/auth_local.py backend/account_routes.py backend/request_security.py backend/app.py backend/db.py backend/tests
git commit -m "security: strengthen backend authentication"
git push origin main
```

---

### Task 7: Full Verification and Final Publication

**Files:**
- Modify only if verification exposes an in-scope regression

**Interfaces:**
- Consumes: all preceding commits
- Produces: a clean, pushed `main` branch with no uncommitted generated artifacts

- [ ] **Step 1: Run static checks**

```bash
.venv/bin/ruff check .
.venv/bin/mypy .
```

Expected: both exit 0.

- [ ] **Step 2: Run daemon and acceptance suites**

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python tests/acceptance/runner.py --mock-external
```

Expected: all tests/scenarios pass.

- [ ] **Step 3: Run the backend suite in its isolated environment**

```bash
backend/.venv/bin/python -m pytest backend/tests/ -q
```

Expected: all backend tests pass.

- [ ] **Step 4: Run privacy and security scans**

```bash
rg -n --hidden -g '!AGENTS.md' -g '!CLAUDE.md' -g '!.git/**' '<forbidden-name>|<forbidden-city>|made by <forbidden-name>' .
backend/.venv/bin/pip-audit -r backend/requirements.txt
.venv/bin/pip-audit
gitleaks detect --no-banner --redact --source .
```

Expected: no shipping identity leak, known vulnerable dependency, or committed
secret. If a scanner is unavailable, record that limitation and do not modify
dependency boundaries merely to install it.

- [ ] **Step 5: Confirm Git scope and push**

```bash
git status --short
git log --oneline --decorate -8
git diff origin/main..HEAD --stat
git push origin main
```

Expected: only approved commits are ahead of `origin/main`; `AGENTS.md` remains
untracked and unstaged; push succeeds.

- [ ] **Step 6: Report verification evidence**

Report exact commit hashes, pushed remote/branch, test counts, and any remaining
environment limitation. Do not claim the backend suite or external scanners
passed unless their commands actually ran successfully.
