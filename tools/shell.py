"""General-purpose shell command tool.

Gives Emma the ability to run arbitrary shell commands on the Mac,
making her genuinely Jarvis-level: file operations, launching programs
with arguments, checking system info, running scripts, etc.
"""
from __future__ import annotations

import shlex
import subprocess

import structlog

from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.shell")

_BLOCKED_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "mkfs",
    "dd if=",
    ":(){",
    "fork bomb",
    "> /dev/sda",
    "chmod -R 777 /",
]


@tool()
def run_command(command: str) -> ToolResult:
    """Run a simple shell command on Garcia's Mac and return the output.

    IMPORTANT: Use ONE simple command. Do NOT write multi-line scripts,
    do NOT chain with && or ;, do NOT embed osascript/AppleScript
    inline. If you need multiple steps, call this tool multiple times.

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
    cmd_lower = command.lower()
    for pattern in _BLOCKED_PATTERNS:
        if pattern in cmd_lower:
            return ToolResult(
                False, None,
                "That command looks dangerous — I won't run it.",
                False,
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
