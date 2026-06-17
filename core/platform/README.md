# Platform abstraction layer (Prompt 30)

One switch decides per-OS behavior; the rest of the codebase stays platform-agnostic.

## Feature tiers

- **Tier 1 — Universal** (never touches this layer): voice loop, OpenAI Realtime,
  sub-agent, web search/deep research, social, memory, dictionary, workflows,
  conditionals. Same on macOS / Windows / Linux.
- **Tier 2 — macOS** (`_mac`): AppleScript app control, EventKit, the AX vision layer.
- **Tier 3 — Windows** (`_win`): equivalents — PowerShell, COM (Outlook/OneNote),
  WinRT calendar, UI Automation. **Stubs today; real impls in 30.x.**

## How it works

```python
from core.platform import notes
await notes.get().create(title, body)   # MacNotes on darwin, WinNotes (stub) on win32
```

Each capability module (`notify`, `fs`, `app_control`, `notes`, …) exposes a
`Protocol` + a `get()` factory. The factory is the ONLY `sys.platform` check.
`_mac` may import pyobjc / `actions.macos`; `_win` and `_stub` never do. A Tier-2
call on Windows raises `UnsupportedOnPlatform`, which tools catch → "En este sistema
no tengo <cap> todavía" (no stack trace to the user).

## Migration status (Part B)

Representative slice migrated through the layer (acceptance harness 99/99 green —
**no behavior change on Mac**). Full surface = 24 files / 86 mac calls; the rest is 30.1.

| Old call site | New mac impl | Protocol | Status |
|---|---|---|---|
| `tools/notes_tool.py` (create osascript) | `_mac/notes.py` | `NotesP` | ✅ migrated |
| `tools/timer_tool.py` (`macos.notify`) | `_mac/notify.py` | `NotifyP` | ✅ migrated |
| paths (`~/Library`, `~/.emma`) | `_mac/fs.py` | `FsP` | ✅ abstracted |
| `core/app_router.py` + `actions.macos.open_app` | `_mac/app_control.py` | `AppControlP` | ✅ wrapped |
| `tools/calendar_tool.py`, `actions/calendar_store.py` | `_mac/calendar.py` | `CalendarP` | ⏳ 30.1 |
| `tools/mail_tool.py`, `messages_tool.py` | `_mac/mail.py` | `MailP` | ⏳ 30.1 |
| `tools/reminders_tool.py` | `_mac/reminders.py` | `RemindersP` | ⏳ 30.1 |
| `core/screen_vision.py` (AX) | `_mac/screen.py` | `ScreenP` | ⏳ 30.1 |
| `core/permissions.py`, `core/background.py` daemon | `_mac/daemon.py` | `DaemonP` | ⏳ 30.1 |
| `tools/{finder,safari,ide_actions,terminal_actions,user_browser,shell,dev,file_ops,tableplus}` | `_mac/*` | various | ⏳ 30.1 |

## Follow-ups

- **30.1** — migrate the remaining ~20 Mac call sites under the layer (calendar, mail,
  reminders, screen/AX, daemon, the misc app tools).
- **30.2+** — real Windows Tier-3 impls: PowerShell app control, Outlook/OneNote COM,
  WinRT calendar, UI Automation screen reading, Credential Manager via `keyring`,
  Windows notifications (toast).
