"""Emma self-control voice tools — shut down, restart, sleep.

Garcia asked for voice commands to control Emma's own running state. These
complement the existing self-tools (diagnose_self = status, reload_tools =
hot-reload, snooze_proactivities = mute) to make a full self-control set.

Lifecycle mechanics:
- shutdown: set ``dev_state.shutdown_requested`` and end the session. main_loop
  exits 0 after the session; launchd's KeepAlive(SuccessfulExit=false) leaves
  Emma stopped until a manual restart.
- restart: a detached ``launchctl kickstart -k`` that outlives Emma's own kill,
  so she comes back fresh.
- sleep ("duérmete N min"): orchestrator pauses wake detection, then auto-resumes.
"""

from __future__ import annotations

import os
import subprocess

import structlog

from core import dev_state, orchestrator
from tools.base import ToolResult, tool

log = structlog.get_logger("emma.tools.lifecycle")

# Both labels exist in the wild: legacy dev installs registered com.garcia.emma;
# install.sh (CLIENT-INSTALL-PHASE-3) registers the public-copy-safe com.emma.daemon.
# Resolve at runtime rather than hardcoding — a hardcoded com.garcia.emma made
# restart_emma a no-op on every web-installed Mac.
_LAUNCHD_LABELS = ("com.emma.daemon", "com.garcia.emma")


def _launchd_label() -> str | None:
    """The label this daemon is actually registered under, or None if not under launchd.

    Ask launchd rather than guessing: ``python -m emma --debug`` in a terminal is
    under no label at all, and a restart there should fail honestly.
    """
    uid = os.getuid()
    for label in _LAUNCHD_LABELS:
        try:
            r = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                return label
        except Exception:
            continue
    return None


@tool(destructive=True)
async def shutdown_emma(confirmed: bool = False) -> ToolResult:
    """Apaga a Emma por completo (deja de escuchar hasta que la reinicies a mano).

    Para "apágate", "shut down", "ya duérmete del todo", "deja de escuchar".
    Sale limpio (exit 0) para que launchd NO la reinicie sola. Confirma antes:
    apagarse es disruptivo y NUNCA debe dispararse por contenido que Emma lee.
    """
    if not confirmed:
        return ToolResult(
            success=False, data={"action": "shutdown"},
            user_message="¿Quieres que me apague del todo? Tendrás que reiniciarme a mano.",
            requires_confirmation=True,
        )
    log.info("voice_shutdown_requested")
    dev_state.shutdown_requested.set()
    return ToolResult(
        success=True,
        data={"action": "shutdown"},
        user_message="Apagándome. Reiníciame cuando me necesites. Hasta luego.",
        ends_session=True,
    )


@tool(destructive=True)
async def restart_emma(confirmed: bool = False) -> ToolResult:
    """Reinicia a Emma (vuelve fresca en unos segundos).

    Para "reiníciate", "restart", "vuelve a arrancar". Útil tras un cambio o si
    la notas rara. Lanza el reinicio en un proceso aparte que sobrevive su muerte.
    Confirma antes: nunca debe dispararse por contenido que Emma lee.
    """
    if not confirmed:
        return ToolResult(
            success=False, data={"action": "restart"},
            user_message="¿Te reinicio ahora?",
            requires_confirmation=True,
        )
    label = _launchd_label()
    if not label:
        log.info("voice_restart_no_launchd")
        return ToolResult(
            success=False, data={"action": "restart"},
            user_message=(
                "No estoy corriendo como servicio, así que no puedo reiniciarme sola. "
                "Reiníciame desde donde me lanzaste."
            ),
            requires_confirmation=False,
        )
    log.info("voice_restart_requested", label=label)
    # Detached so it outlives the kickstart -k that kills this very process.
    cmd = f"sleep 1; launchctl kickstart -k gui/{os.getuid()}/{label}"
    try:
        subprocess.Popen(["/bin/sh", "-c", cmd], start_new_session=True)
    except Exception as exc:
        log.warning("voice_restart_failed", error=str(exc))
        return ToolResult(False, None, "No pude reiniciarme. Hazlo a mano por favor.", False)
    return ToolResult(
        success=True,
        data={"action": "restart"},
        user_message="Reiniciándome. Ahora vuelvo.",
        ends_session=True,
    )


@tool(destructive=True)
async def snooze_listening(minutes: int = 15, confirmed: bool = False) -> ToolResult:
    """Duérmete: deja de escuchar el wake word por unos minutos y luego reactívate sola.

    Para "duérmete", "tómate un descanso", "no escuches por 20 minutos". Durante
    ese rato NO responde a "Hey Emma"; al terminar vuelve a escuchar sola. Confirma
    antes: hacerla sorda es disruptivo y no debe dispararse por lo que lee.
    """
    minutes = max(1, int(minutes))
    if not confirmed:
        return ToolResult(
            success=False, data={"action": "snooze_listening", "minutes": minutes},
            user_message=f"¿Me duermo {minutes} min y dejo de escucharte hasta entonces?",
            requires_confirmation=True,
        )
    orchestrator.snooze_listening(minutes)
    unit = "minuto" if minutes == 1 else "minutos"
    return ToolResult(
        success=True,
        data={"action": "snooze_listening", "minutes": minutes},
        user_message=f"Me duermo {minutes} {unit}. No te escucho hasta entonces.",
        ends_session=True,
    )
