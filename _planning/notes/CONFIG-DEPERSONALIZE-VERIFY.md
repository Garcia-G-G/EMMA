# CONFIG-DEPERSONALIZE — verification note (2026-07-20)

Stripped the maker's personal data from the public repo's shipped config and
closed the README doc-drift. Pre-flight verified every premise against the real
files before editing.

## What changed

- **`config/dictionary.toml`** — `[user].github_username` blanked; `[contacts.mom]`
  (Ana García) replaced with a commented generic template; all four `[facts.00N]`
  removed (empty `[facts]` table kept, with a comment that facts are voice-learned);
  `[connections.learning-rots-local]` removed; "Garcia" stripped from the OWASP and
  ELIMINAR `context` strings and from every section comment. `[apps]` defaults
  (Cursor/Brave/iTerm/Spotify) **kept** as fallbacks (Garcia's call, 2026-07-20) with
  the comment reworded to "the user's preferred (auto-detected; these are fallbacks)".
- **`config/vocabulary.toml`** — `[Garcia]` and `[Monterrey]` removed; `[Emma]`
  description → "The user's assistant"; and the five runtime-learned entries the
  2026-07-17 grep missed (`NillOjeda` — a person's name, `limitacinterminalCursor`,
  `ObservacincierrepestaafallidoenChrome`, `cloud`, `reachy`) removed (Garcia's call,
  2026-07-20 — "remove all 5"). Tech terms (Pydantic, MCP, wake-word aliases) intact.
- **`config/app_capabilities.toml`** — 4 "Garcia" mentions (2 `notes=` values, 2
  comments) reworded to "the user"; these are in `config/*.toml` and so fall under
  DoD #1's grep. No Keychain string here — safe rewords.
- **`README.md`** — added the `curl … install.sh` command near the top; tool count
  70+ → 180+ (verified: 185 `@tool()` decorators, 183 registered); wake-word section
  rewritten from the obsolete openWakeWord-training flow to the shipped Vosk reality
  (offline, ships with installer, "Hey/Oye Emma"), openWakeWord kept only as an
  advanced swap; launchctl label `com.garcia.emma` → `com.emma.daemon`; Secret-tier
  Keychain cell keeps `com.garcia.emma` with a footnote marking it a frozen internal
  identifier.

## Verified frozen (untouched)

`EMMA_KEYCHAIN_SERVICE` and every `com.garcia.emma` Keychain reference in
`config/settings.py:156`, `core/secrets.py:27`, `core/pairing.py` — renaming would
orphan existing users' stored secrets. `git diff` on those files shows no
Keychain-string edits.

## KNOWN ISSUE — write-path clobber (deferred, do NOT fix here)

**Confirmed real.** `core/dictionary.py:24` resolves `_DICT_PATH` to
`<repo>/config/dictionary.toml`, so `remember_user_profile`/`remember_fact`/etc.
write **inside the installed source tree** (`~/.emma/src/config/`). The installer
(`_landing/install.sh:101`) re-extracts the tarball with
`tar -xzf … --strip-components=1 -C ~/.emma/src`, and `config/dictionary.toml` is in
the tarball — so **re-running `install.sh` overwrites a user's voice-learned facts**.
(The same happened to this repo: Emma's runtime-learned vocabulary terms landed in
the committed `config/vocabulary.toml`.)

Fix belongs in a future **CONFIG-USERDIR** prompt: move the write-path to
`~/.emma/dictionary.toml` (user-data dir), read personal-over-template. Garcia
deferred the split (2026-07-17); flagged here so it isn't lost.
