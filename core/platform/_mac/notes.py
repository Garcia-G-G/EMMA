"""macOS notes (Prompt 30) — the Apple Notes osascript, moved out of notes_tool.

Still goes through ``actions.macos`` (esc/osascript), so the acceptance harness's
external mocks intercept exactly as before — no behavior change on Mac.
"""

from __future__ import annotations

_NOTES_TIMEOUT_S = 15.0


class MacNotes:
    async def create(self, title: str, body: str, folder: str = "") -> None:
        """Create a note. Propagates ``macos.AppleScriptError`` on failure."""
        from actions import macos

        t = macos.esc_applescript(title)
        b = macos.esc_applescript(body)
        # HTML body keeps the title (first line) separate from the body so exact-title
        # lookup in read/delete keeps working (19.4-followup behavior, preserved).
        html_body = f"<div>{t}</div><div>{b}</div>" if body else f"<div>{t}</div>"
        note_props = f'{{body:"{html_body}"}}'
        if folder:
            f = macos.esc_applescript(folder)
            make = (
                f'if not (exists folder "{f}") then make new folder with properties {{name:"{f}"}}\n'
                f'tell folder "{f}" to make new note with properties {note_props}'
            )
        else:
            make = f"make new note with properties {note_props}"
        script = f'tell application "Notes"\n{make}\nend tell'
        await macos.osascript(script, timeout_s=_NOTES_TIMEOUT_S)
