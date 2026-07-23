# Emma Security, Privacy, and Reliability Hardening Design

## Objective

Improve Emma's security, privacy, and runtime reliability without broad
architectural rewrites. The work will be delivered as independently tested,
reviewable commits and pushed to the current GitHub branch after verification.

## Scope

The change set has four blocks:

1. Remove public and internal assumptions about the maker's identity or
   location, and make the atom Emma's consistent visual identity.
2. Fix confirmed correctness and asynchronous cleanup defects.
3. Harden password and cookie-based authentication.
4. Run repository-wide verification and publish each meaningful block.

Large-scale decomposition of `core/conversation.py`, `backend/db.py`, and other
oversized modules is explicitly out of scope. Those changes are valuable only
when tied to a concrete feature or defect and would add unnecessary regression
risk here.

## Global Constraints

- Public copy must never identify the maker or claim a geographic origin.
- Code comments, prompts, fixtures, examples, and generated assets must use
  generic user language rather than a real identity or location.
- The atom is Emma's visual mark. The rejected Three.js wireframe and plain
  sphere directions must not remain in shipping surfaces.
- Secret-tier values must never enter SQLite memory, logs, prompts, fixtures,
  or committed files.
- No new macOS permissions are introduced.
- No new long-running synchronous tool behavior is introduced.
- Existing untracked or unrelated user changes must be preserved.
- Each implementation block must have focused regression tests and pass the
  relevant existing suite before it is committed or pushed.

## Block 1: Identity and Public-Copy Privacy

### Behavior

Replace maker-specific names, possessives, locations, and narratives with
neutral terms such as "the user", "your Mac", or clearly fictional placeholders.
Remove optional author metadata from `pyproject.toml`; Python packaging metadata
does not require an `authors` field.

Sanitize:

- Public documentation and package metadata.
- Installer metadata, sample phrases, and public HTML.
- Runtime prompts and tool descriptions.
- Code comments and docstrings.
- Generated capability documentation.
- Tests and acceptance fixtures that encode the real identity or location.

Historical repository instructions may describe forbidden examples so the rule
can be enforced, but shipping product copy and implementation files may not
repeat or rely on those details.

### Visual identity

Replace the dashboard's Three.js triangulated wireframe with an inline
SVG/CSS atom: clean orbit rings, a glowing nucleus, and small electrons. The
visualizer must preserve its existing state/event integration and avoid adding
an image asset, WebGL dependency, or new network request.

### Verification

Add or extend invariant tests that scan shipping surfaces for forbidden
identity/location patterns and rejected visual directions. Regenerate
`self/capabilities.md` through its supported generator, then verify it is clean.

## Block 2: Correctness and Async Reliability

### Memory supersession review

Fix the duplicated `FROM` clause in the recent-supersessions query. Add a
regression test that creates an old fact and its replacement, calls the public
review function, and checks the returned pair and ordering.

### Realtime task cleanup

For every manually created pump/timer task:

- Cancel unfinished tasks when the first task completes.
- Await both completed and cancelled tasks with `return_exceptions=True`.
- Preserve cleanup in `finally` blocks.
- Do not swallow cancellation in a way that leaves a task or WebSocket alive.

The daemon and public demo proxy paths should follow the same lifecycle
invariant even if their authorization and metering behavior differ.

### HTTP client lifecycle

Use `async with httpx.AsyncClient(...)` for request-scoped clients. Use FastAPI
lifespan only for resources that are intentionally shared for the full
application lifetime. No response path may leave an unclosed client.

### Verification

Add focused tests for SQL execution, task cancellation/awaiting, and HTTP client
closure. Existing WebSocket metering and close behavior must remain unchanged.

## Block 3: Authentication Hardening

### Password hashing

Raise new PBKDF2-HMAC-SHA256 hashes from 240,000 to 600,000 iterations, matching
current OWASP guidance. Keep the iteration count encoded in every stored hash.

After a successful login with an older valid hash, transparently generate and
store a current-strength hash. Failed authentication must never rewrite a hash.

Reject excessively long passwords before invoking PBKDF2 to bound server CPU
work. The same bound applies consistently to registration, login, reset, and
password-change paths. Existing valid passwords within the bound remain
compatible.

### Cookie and request integrity

Retain `HttpOnly`, `Secure` on HTTPS, host-only scope, and an explicit SameSite
policy. Use a production `__Host-` session-cookie name where the deployment is
HTTPS, with `Path=/` and no `Domain`; retain a development-compatible cookie
name for local HTTP tests.

Protect unsafe cookie-authenticated requests (`POST`, `PUT`, `PATCH`, `DELETE`)
by validating their `Origin` against the configured public origin. Requests
authenticated exclusively by a bearer device token are outside this browser
CSRF boundary. Development behavior must remain usable on the configured local
origin.

Logout and other session-changing responses must use `Cache-Control: no-store`.
Cookie migration must clear the legacy cookie during login/logout so existing
users are not stranded.

### Verification

Tests must cover:

- New hashes use 600,000 iterations.
- Legacy hashes verify and upgrade only after successful login.
- Overlong passwords are rejected before expensive hashing.
- Production cookies have the required attributes.
- Same-origin mutations succeed.
- Cross-origin cookie-authenticated mutations fail.
- Bearer-authenticated device traffic remains unaffected.
- Legacy cookies are cleared during migration.

## Block 4: Verification and Publishing

Run, at minimum:

1. Focused tests for each changed subsystem.
2. `.venv/bin/ruff check .`
3. `.venv/bin/mypy .`
4. `.venv/bin/python -m pytest tests/ -q`
5. Backend tests in the backend dependency environment.
6. The mock acceptance suite.
7. A repository-wide privacy invariant scan.
8. Available dependency and secret scanners without committing scanner output.

Backend dependencies remain isolated from the daemon's `pyproject.toml`.
If the existing environment lacks them, create or use a temporary environment
rather than adding backend-only packages to the daemon.

Commit and push after each independently passing block:

1. `privacy: remove personal identity from shipping surfaces`
2. `fix: harden memory and async resource lifecycles`
3. `security: strengthen backend authentication`
4. A final verification/documentation commit only if verification requires
   tracked changes.

Push only commits created for this approved scope. Do not add the untracked
repository guidance file or unrelated working-tree changes.

## Failure Handling and Rollback

Every block is independently revertible. If a block fails its focused tests, it
will not be committed or pushed. If the full suite reveals cross-block
interaction, fix the owning block and rerun both focused and full verification
before publishing further work.

Network-dependent tests must use mocks. No production deployment, account
mutation, billing action, or secret rotation is part of this design.

## Research Basis

- OWASP Password Storage guidance recommends PBKDF2-HMAC-SHA256 with at least
  600,000 iterations.
- OWASP session and CSRF guidance recommends explicit secure cookie attributes
  plus request-integrity defenses rather than relying on cookie defaults alone.
- Python's asyncio documentation requires cancelled tasks to be given an
  opportunity to run cleanup and recommends structured task lifecycles.
- FastAPI recommends lifespan context managers for application-lifetime
  resources.
- Python packaging metadata defines `authors` as optional.
