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

_LAUNCHD_LABEL = "com.garcia.emma"


@tool()
async def shutdown_emma() -> ToolResult:
    """Apaga a Emma por completo (deja de escuchar hasta que la reinicies a mano).

    Para "apágate", "shut down", "ya duérmete del todo", "deja de escuchar".
    Sale limpio (exit 0) para que launchd NO la reinicie sola.
    """
    log.info("voice_shutdown_requested")
    dev_state.shutdown_requested.set()
    return ToolResult(
        success=True,
        data={"action": "shutdown"},
        user_message="Apagándome. Reiníciame cuando me necesites. Hasta luego.",
        ends_session=True,
    )


@tool()
async def restart_emma() -> ToolResult:
    """Reinicia a Emma (vuelve fresca en unos segundos).

    Para "reiníciate", "restart", "vuelve a arrancar". Útil tras un cambio o si
    la notas rara. Lanza el reinicio en un proceso aparte que sobrevive su muerte.
    """
    log.info("voice_restart_requested")
    # Detached so it outlives the kickstart -k that kills this very process.
    cmd = f"sleep 1; launchctl kickstart -k gui/{os.getuid()}/{_LAUNCHD_LABEL}"
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


@tool()
async def snooze_listening(minutes: int = 15) -> ToolResult:
    """Duérmete: deja de escuchar el wake word por unos minutos y luego reactívate sola.

    Para "duérmete", "tómate un descanso", "no escuches por 20 minutos". Durante
    ese rato NO responde a "Hey Emma"; al terminar vuelve a escuchar sola.
    """
    minutes = max(1, int(minutes))
    orchestrator.snooze_listening(minutes)
    unit = "minuto" if minutes == 1 else "minutos"
    return ToolResult(
        success=True,
        data={"action": "snooze_listening", "minutes": minutes},
        user_message=f"Me duermo {minutes} {unit}. No te escucho hasta entonces.",
        ends_session=True,
    )
