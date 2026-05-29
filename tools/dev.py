"""Developer-mode voice tools.

`open_workspace_for_debugging` now routes through environment
detection. When no supported IDE is installed, the tool offers to
install VS Code (the project's recommended editor) and, on confirmation,
runs the full install + duti default-handler set + workspace-open flow.
A declined offer is recorded; after two declines Emma stops asking and
silently falls back to Finder.

Resuming from dev mode stays manual: the banner Emma opens prints the
exact ``launchctl`` command.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

import structlog

from actions import environment, macos
from actions.environment import (
    INSTALL_RECOMMENDATIONS,
    DetectionResult,
    decline_count,
    detect_preferred,
    ensure_duti,
    have_brew,
    install_cask,
    record_decline,
    set_ide_default,
    smoke_launch,
)
from core import dev_state, runtime
from tools.base import ToolResult, tool
from tools.diagnostics import health_check

log = structlog.get_logger("emma.tools.dev")

REPO_ROOT = Path(__file__).resolve().parent.parent

_APP_NAME_BY_KEY: dict[str, str] = {
    "cursor": "Cursor",
    "code": "Visual Studio Code",
    "zed": "Zed",
    "subl": "Sublime Text",
}


def _git(args: list[str]) -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), *args],
            text=True,
            timeout=5,
            stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except Exception:
        return "<unknown>"


def _build_banner(branch: str, last_commit: str, restart_cmd: str) -> str:
    border = "+" + "-" * 58 + "+"
    return (
        f"\n{border}\n"
        f"|  EMMA DEV MODE\n"
        f"|\n"
        f"|  Repo:   {REPO_ROOT}\n"
        f"|  Branch: {branch}\n"
        f"|  Last:   {last_commit}\n"
        f"|\n"
        f"|  Resume Emma when you're done editing:\n"
        f"|    {restart_cmd}\n"
        f"{border}\n"
    )


def _open_editor_at(ide: DetectionResult, repo: Path) -> None:
    if ide.binary_path:
        try:
            subprocess.Popen(
                [ide.binary_path, str(repo)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception as exc:
            log.warning("editor_binary_failed", binary=ide.binary_path, error=str(exc))
    app_label = _APP_NAME_BY_KEY.get(ide.app_name or "")
    if app_label:
        try:
            subprocess.Popen(
                ["open", "-a", app_label, str(repo)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception as exc:
            log.warning("editor_open_a_failed", app=app_label, error=str(exc))
    subprocess.run(["open", "-R", str(repo)], check=False)


def _open_dev_terminal(banner_path: Path) -> None:
    cd_cmd = f"cd {shlex.quote(str(REPO_ROOT))} && clear && cat {shlex.quote(str(banner_path))}"
    try:
        macos.run_applescript(
            f'tell application "Terminal" to activate\n'
            f'tell application "Terminal" to do script "{cd_cmd}"'
        )
    except macos.AppleScriptError as exc:
        log.error("dev_terminal_open_failed", error=str(exc))


def _shutdown_service() -> None:
    uid = os.getuid()
    with contextlib.suppress(Exception):
        subprocess.run(
            ["launchctl", "disable", f"gui/{uid}/com.garcia.emma"],
            check=False,
            timeout=5,
        )
    dev_state.shutdown_requested.set()


def _restart_cmd() -> str:
    uid = os.getuid()
    return (
        f"launchctl enable gui/{uid}/com.garcia.emma && "
        f"launchctl kickstart -k gui/{uid}/com.garcia.emma"
    )


def _open_workspace(ide: DetectionResult, user_message: str) -> ToolResult:
    branch = _git(["branch", "--show-current"]) or "<detached>"
    last_commit = _git(["log", "-1", "--oneline"])
    restart_cmd = _restart_cmd()
    banner = _build_banner(branch, last_commit, restart_cmd)
    banner_path = Path(tempfile.mkstemp(prefix="emma_devmode_", suffix=".txt")[1])
    banner_path.write_text(banner)

    _open_dev_terminal(banner_path)
    _open_editor_at(ide, REPO_ROOT)
    _shutdown_service()

    log.info("dev_mode_open", repo=str(REPO_ROOT), branch=branch, ide=ide.app_name)
    return ToolResult(
        success=True,
        data={
            "repo": str(REPO_ROOT),
            "branch": branch,
            "restart_cmd": restart_cmd,
            "ide": ide.app_name,
            "is_user_override": ide.is_user_override,
        },
        user_message=user_message,
        requires_confirmation=False,
    )


def _open_with_finder(spoken_lang: str) -> ToolResult:
    """Fallback when no IDE is available and we won't pester for install."""
    branch = _git(["branch", "--show-current"]) or "<detached>"
    last_commit = _git(["log", "-1", "--oneline"])
    restart_cmd = _restart_cmd()
    banner = _build_banner(branch, last_commit, restart_cmd)
    banner_path = Path(tempfile.mkstemp(prefix="emma_devmode_", suffix=".txt")[1])
    banner_path.write_text(banner)

    _open_dev_terminal(banner_path)
    subprocess.run(["open", "-R", str(REPO_ROOT)], check=False)
    _shutdown_service()

    msg = (
        "Sin editor instalado abro el proyecto en Finder. Me detengo hasta que reinicies."
        if spoken_lang == "es"
        else "No editor installed - opened the project in Finder. I'll stop until you restart me."
    )
    return ToolResult(
        success=True,
        data={"repo": str(REPO_ROOT), "ide": None, "fallback": "finder"},
        user_message=msg,
        requires_confirmation=False,
    )


def _offer_install_ide(spoken_lang: str) -> ToolResult:
    rec = INSTALL_RECOMMENDATIONS["ide"]
    if spoken_lang == "es":
        msg = (
            "Para abrir mi código necesito un editor instalado. "
            "No detecto Cursor, VS Code, Zed ni Sublime. "
            f"Te recomiendo {rec['human']}, es gratis. "
            "Si me dices que sí, lo instalo y te lo dejo como default para abrir código. "
            "¿Le entramos?"
        )
    else:
        msg = (
            "I need a code editor installed to open my source. "
            "I don't see Cursor, VS Code, Zed, or Sublime. "
            f"I recommend {rec['human']}, it's free. "
            "If you say yes I'll install it and set it as your default for source files. "
            "Sound good?"
        )
    return ToolResult(
        success=True,
        data={"recommend": rec},
        user_message=msg,
        requires_confirmation=True,
    )


def _guide_brew_install(spoken_lang: str) -> ToolResult:
    with contextlib.suppress(macos.AppleScriptError):
        macos.open_url("https://brew.sh/")
    if spoken_lang == "es":
        msg = (
            "Necesito Homebrew primero. Te abrí brew punto sh; instálalo con el "
            "comando que ves ahí y vuelve a pedirme lo mismo cuando termines."
        )
    else:
        msg = (
            "I need Homebrew first. I opened brew.sh - install it with the command "
            "shown there, then ask me again when you're done."
        )
    return ToolResult(False, None, msg, False)


async def _install_ide_and_set_defaults(spoken_lang: str) -> tuple[bool, str]:
    """Heavy-lift: install VS Code via brew, install duti, set duti defaults."""
    rec = INSTALL_RECOMMENDATIONS["ide"]

    if not await ensure_duti(spoken_lang=spoken_lang):
        return False, (
            "No pude instalar duti." if spoken_lang == "es" else "Couldn't install duti."
        )

    ok, _ = await install_cask(rec["cask"], spoken_lang=spoken_lang)
    if not ok:
        return False, (
            f"La instalación de {rec['human']} falló."
            if spoken_lang == "es"
            else f"Install of {rec['human']} failed."
        )

    smoke_launch(rec["human"])

    entry = next((e for e in environment.IDE_SHORTLIST if e["key"] == rec["key"]), None)
    if entry is None:
        log.error("ide_shortlist_missing_key", key=rec["key"])
        return False, "No encontré el editor recomendado en la lista."
    duti_result = set_ide_default(entry["bundle"])
    log.info("duti_set_ide_default", **duti_result)

    return True, ""


@tool()
async def open_workspace_for_debugging(
    confirmed: bool = False,
    cancelled: bool = False,
) -> ToolResult:
    """Open Emma's source for live editing, then stop the running service.

    Use this when Garcia says any of these (and similar phrasings):

    - "Emma, te voy a debuggear"
    - "voy a hacerte reparaciones"
    - "ábreme tu código"
    - "abre tu workspace"
    - "dev mode"
    - "open your codebase"

    Opens a Terminal at the repo root with a banner showing the repo
    path, current branch, last commit, and the exact command to resume.
    Also opens the user's detected (or override-preferred) editor:
    Cursor > VS Code > Zed > Sublime Text. If none is installed Emma
    offers to install VS Code and set it as the system default for
    source files; if the user declines twice she falls back to Finder.
    """
    spoken_lang = runtime.get_spoken_lang()
    ide = detect_preferred("ide")

    if cancelled:
        record_decline("ide")
        log.info("ide_install_declined", count=decline_count("ide"))
        return _open_with_finder(spoken_lang)

    if ide.app_name is None:
        if decline_count("ide") >= 2:
            log.info("ide_install_skipped_two_declines")
            return _open_with_finder(spoken_lang)

        if not confirmed:
            return _offer_install_ide(spoken_lang)

        # Confirmed install path.
        if not have_brew():
            return _guide_brew_install(spoken_lang)

        ok, err = await _install_ide_and_set_defaults(spoken_lang)
        if not ok:
            return ToolResult(False, None, err, False)

        ide = detect_preferred("ide", force_refresh=True)
        if ide.app_name is None:
            msg = (
                "Instalé VS Code pero no la detecto. Reinicia y vuelve a intentar."
                if spoken_lang == "es"
                else "Installed VS Code but couldn't detect it. Restart and try again."
            )
            return ToolResult(False, None, msg, False)

        if spoken_lang == "es":
            confirm = (
                f"Listo, instalé {INSTALL_RECOMMENDATIONS['ide']['human']} y la dejé como default "
                "para tus archivos de código. Abriendo el proyecto. Me detengo hasta que reinicies."
            )
        else:
            confirm = (
                f"Done - installed {INSTALL_RECOMMENDATIONS['ide']['human']} and set it as default "
                "for your source files. Opening the project. I'll stop until you restart me."
            )
        return _open_workspace(ide, confirm)

    # Happy path: IDE already available.
    msg = (
        "Abriendo mi código. Me detengo hasta que reinicies el servicio."
        if spoken_lang == "es"
        else "Opening my source. I'll stop until you restart the service."
    )
    return _open_workspace(ide, msg)


@tool()
async def describe_my_health() -> ToolResult:
    """Run the health check and speak the result.

    Use when Garcia says:

    - "Emma, ¿cómo te sientes?"
    - "¿estás bien?"
    - "diagnóstico"
    - "health check"
    """
    return await health_check()
