"""The tool loader: brings a non-core capability into the session (LAUNCH-12).

Only ~24 core tools are advertised up front. Everything else is reachable
through this one: the model describes what it needs, ``core.tool_selection``
retrieves the matching tools and pushes them into the live session, and the
model calls the real tool in its next step.
"""

from __future__ import annotations

from core import tool_selection
from tools.base import ToolResult, tool


@tool()
async def find_tools(need: str) -> ToolResult:
    """Load more tools. Call this FIRST whenever none of your current tools fits the user's request.

    `need` = the user's request in a few words, in their language, e.g. 'mandar un
    WhatsApp a Juan', 'agregar a mi nota Compras', 'cerrar pestañas duplicadas'.
    Emma has ~170 tools (notes, mail, calendar, reminders, messaging, social,
    files, IDE, terminal, browser tabs, timers, GitHub, coding agents…); only a
    core is loaded until you ask. Do not say anything to the user about this
    step — call the tool it returns right away.
    """
    loaded = await tool_selection.load(need)
    if not loaded:
        return ToolResult(
            success=False,
            data={"loaded": []},
            user_message=(
                "No encontré una herramienta para eso. Dile al usuario brevemente que "
                "no puedes hacerlo, o pídele que lo diga de otra forma."
            ),
        )
    return ToolResult(
        success=True,
        data={"loaded": loaded},
        user_message=(
            "Tools now available: "
            + ", ".join(loaded)
            + ". Call the one that fits the user's request now; don't mention this step."
        ),
    )
