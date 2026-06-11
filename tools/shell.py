"""General-purpose shell command tool.

Gives Emma the ability to run arbitrary shell commands on the Mac,
making her genuinely Jarvis-level: file operations, launching programs
with arguments, checking system info, running scripts, etc.
"""

from __future__ import annotations

import re
import subprocess

import structlog

from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.shell")

# Catastrophic commands that are ALWAYS refused, even if confirmed — there is
# no legitimate voice-assistant reason to run these. Each rule carries a short
# Spanish REASON: the refusal surfaces the reason, never the matched command
# text. A blocked command can carry a secret on its line (`curl -u u:pass …`),
# and the message is spoken, sent to the LLM, and shown on the dashboard —
# echoing the fragment would leak it (audit fix).
_BLOCKED_RULES = [
    (r"\bmkfs", "formatear un disco"),
    (r"\bdd\s+if=", "escritura cruda a disco (dd)"),
    # dd writing to a /dev device with args in any order (of= before if=, or a
    # `< /dev/…` input redirect) — a confirmed disk wipe must stay unreachable.
    (r"\bdd\b.*\bof=\s*/dev/(r?disk|sd)", "escritura cruda a disco (dd)"),
    (r":\(\)\s*\{", "fork bomb"),
    (r">\s*/dev/sd", "escritura a un dispositivo de disco"),
    (r">\s*/dev/disk", "escritura a un dispositivo de disco"),
    (r"\bchmod\s+(-[a-z]*R[a-z]*\s+)?777\s+/", "permisos 777 en la raíz"),
    (r"\b(curl|wget)\b.*\|\s*(ba)?sh", "descargar y ejecutar desde internet"),
    # process substitution: `bash <(curl …)` is pipe-to-shell without a pipe.
    (r"<\(\s*(curl|wget)\b", "ejecutar algo descargado de internet"),
    # generic pipe-to-shell: ANY `… | sh|bash|zsh`. `\bsh\b` keeps `| sshpass`
    # / `| shasum` from matching.
    (r"\|\s*(ba|z)?sh\b", "tubería hacia una shell"),
    # eval/source of a remote URL — a classic one-liner remote-exec. (Bare
    # `. http://…` is intentionally NOT blocked: `cd . ; …` would false-positive.)
    (r"\beval\b.*https?://", "ejecutar una URL remota (eval)"),
    (r"\bsource\b.*https?://", "ejecutar una URL remota (source)"),
    # bash history expansion: `!!` / `!$` / `!N` could resurrect a deleted command.
    (r"!!", "expansión de historial"),
    (r"!\$", "expansión de historial"),
    (r"!\d", "expansión de historial"),
]
_BLOCKED_RE = [(re.compile(p, re.IGNORECASE), reason) for p, reason in _BLOCKED_RULES]

# Commands that mutate the filesystem / processes / system state and so must be
# confirmed by voice before running. The blocklist above is NOT a security
# boundary (it is trivially bypassable: `rm -rf $HOME`, `find ~ -delete`); the
# confirmation gate is. Matched as whole words so `format` inside a path or
# `removed` in output don't trip it.
_DESTRUCTIVE_PATTERNS = [
    r"\brm\b", r"\brmdir\b", r"\bunlink\b", r"\bshred\b", r"\bsrm\b",
    r"\bmv\b", r"\bdd\b", r"\btrash\b",
    r"\bchmod\b", r"\bchown\b", r"\bchgrp\b", r"\bchflags\b",
    r"\bkill\b", r"\bkillall\b", r"\bpkill\b",
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b",
    r"\bdiskutil\b", r"\bfdisk\b", r"\bnvram\b",
    r"\bsudo\b", r"\bsu\b",
    r"\bdefaults\s+delete\b",
    r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\b", r"--force\b", r"\bgit\s+push\b",
    r"\b(brew|pip|pip3|npm|uv)\s+(uninstall|remove|rm)\b",
    r"\bfind\b.*-delete\b", r"\bfind\b.*-exec\b",
    # truncating redirect `> file` (not >>, 2>, &>, >&) — and not the `=>`/`->`/`>=`
    # arrows that show up when Garcia asks Emma to echo or grep code.
    r"(?<![0-9&>=-])>(?![>&=])",
]
_DESTRUCTIVE_RE = [re.compile(p, re.IGNORECASE) for p in _DESTRUCTIVE_PATTERNS]


def _is_destructive(command: str) -> bool:
    return any(p.search(command) for p in _DESTRUCTIVE_RE)


@tool()
def run_command(command: str, confirmed: bool = False) -> ToolResult:
    """Run a simple shell command on Garcia's Mac and return the output.

    IMPORTANT: Use ONE simple command. Do NOT write multi-line scripts,
    do NOT chain with && or ;, do NOT embed osascript/AppleScript
    inline. If you need multiple steps, call this tool multiple times.

    Destructive commands (rm, mv, chmod, kill, sudo, disk/power ops, force
    pushes, truncating redirects, …) require a spoken confirmation before they
    run; read-only commands run immediately.

    Good examples:
    - run_command("code ~/Documents/EMMA")
    - run_command("df -h /")
    - run_command("mkdir ~/Desktop/work")
    - run_command("curl -s ifconfig.me")
    - run_command("killall Spotify")
    - run_command("ls ~/Desktop")
    - run_command("open -a Terminal")
    - run_command("networksetup -getairportnetwork en0")
    - run_command("top -l 1 | head -10")
    """
    # Pre-execution refusal: the blocklist is checked BEFORE the command is ever
    # handed to the shell. We tell Garcia the REASON (e.g. "tubería hacia una
    # shell"), never the matched command text — the command can carry a secret.
    for pattern, reason in _BLOCKED_RE:
        if pattern.search(command):
            return ToolResult(
                False,
                None,
                f"No ejecuto eso por seguridad: {reason}.",
                False,
            )

    # Two-phase confirmation gate for state-mutating commands.
    if _is_destructive(command) and not confirmed:
        return ToolResult(
            success=False,
            data={"command": command},
            user_message=f"Voy a ejecutar: {command}. ¿Lo confirmo?",
            requires_confirmation=True,
        )

    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd="/Users/go",
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        if proc.returncode != 0:
            output = stderr or stdout or f"Exit code {proc.returncode}"
            log.warning("shell_cmd_failed", cmd=command, code=proc.returncode)
            return ToolResult(
                False,
                {"exit_code": proc.returncode, "output": output},
                f"Command failed: {output[:200]}",
                False,
            )

        output = stdout or "(no output)"
        log.info("shell_cmd_ok", cmd=command)
        return ToolResult(
            True,
            {"exit_code": 0, "output": output},
            output[:300] if len(output) <= 300 else output[:297] + "...",
            False,
        )

    except subprocess.TimeoutExpired:
        log.warning("shell_cmd_timeout", cmd=command)
        return ToolResult(False, None, "Command timed out after 30 seconds.", False)
    except Exception as exc:
        log.error("shell_cmd_error", cmd=command, error=str(exc))
        return ToolResult(False, None, f"Error: {exc}", False)
