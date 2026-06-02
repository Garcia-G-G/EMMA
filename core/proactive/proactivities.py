"""All the proactivities, registered with the engine.

Adding one: write an async function returning a ``ProactiveEvent`` (or ``None``
to skip), decorate with ``@scheduled`` or ``@polled``, and add its
``PROACTIVE_*`` flag in ``config/settings.py``. Each delegates to existing tools
(calendar/mail/memory/reminders) and only synthesizes the spoken line.
"""

from __future__ import annotations

import datetime as dt
from itertools import pairwise

import structlog

from config.settings import settings
from core.proactive.triggers import polled, scheduled
from core.proactive.types import Priority, ProactiveEvent

log = structlog.get_logger("emma.proactive.proactivities")

_DAYS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MONTHS_ES = [
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
]


def _today_es() -> str:
    now = dt.datetime.now()
    return f"{_DAYS_ES[now.weekday()]} {now.day} de {_MONTHS_ES[now.month - 1]}"


async def _synthesize_es(prompt: str, max_tokens: int = 200) -> str:
    """One cheap gpt-4o-mini completion → a Spanish line. '' on failure."""
    if not settings.OPENAI_API_KEY:
        return ""
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        rsp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.4,
        )
        return (rsp.choices[0].message.content or "").strip()
    except Exception as exc:
        log.error("proactive_synthesize_failed", error=str(exc))
        return ""


# ---- C.1 Morning briefing -------------------------------------------------
@scheduled(
    "morning_briefing", settings.PROACTIVE_MORNING_BRIEFING_CRON, "PROACTIVE_MORNING_BRIEFING"
)
async def morning_briefing() -> ProactiveEvent | None:
    from tools.calendar_tool import today_events
    from tools.mail_tool import list_unread

    cal = await today_events()
    mail = await list_unread(limit=3)
    text = await _synthesize_es(
        "Sintetiza en 2-3 oraciones un briefing matinal en español para Garcia. "
        f"Hoy es {_today_es()}. "
        f"Eventos: {cal.user_message[:400]}. Correos sin leer: {mail.user_message[:400]}."
    )
    if not text:
        # Fallback without the LLM: stitch the tool messages.
        text = f"Buenos días, Garcia. {cal.user_message} {mail.user_message}"
    return ProactiveEvent(
        source="morning_briefing", priority=Priority.SPEAK, summary_es=text[:140], detail=text
    )


# ---- C.2 Meeting prep (5 minutes before) ----------------------------------
_meeting_fired: set[str] = set()


@polled("meeting_prep", 60, "PROACTIVE_MEETING_PREP")
async def meeting_prep() -> ProactiveEvent | None:
    from core.proactive.calendar_events import today_events_raw

    now = dt.datetime.now()
    for ev in await today_events_raw():
        delta = (ev.start - now).total_seconds()
        if 240 <= delta <= 360 and ev.uid not in _meeting_fired:
            _meeting_fired.add(ev.uid)
            return ProactiveEvent(
                source="meeting_prep",
                priority=Priority.SPEAK,
                summary_es=f"En 5 minutos: {ev.title}.",
                detail=f"Garcia, en 5 minutos tienes {ev.title} a las {ev.start.strftime('%H:%M')}.",
                meta={"uid": ev.uid},
            )
    return None


# ---- C.3 End-of-day reflection --------------------------------------------
@scheduled("eod_reflection", settings.PROACTIVE_EOD_REFLECTION_CRON, "PROACTIVE_EOD_REFLECTION")
async def eod_reflection() -> ProactiveEvent | None:
    return ProactiveEvent(
        source="eod_reflection",
        priority=Priority.NOTIFY,
        summary_es="¿Quieres que recuerde algo de hoy?",
        detail="Garcia, ¿quieres que recuerde algo importante de hoy? Dímelo y lo guardo.",
    )


# ---- C.4 Calendar conflict detector ---------------------------------------
@polled("calendar_conflict", 600, "PROACTIVE_CALENDAR_CONFLICT")
async def calendar_conflict() -> ProactiveEvent | None:
    from core.proactive.calendar_events import today_events_raw

    events = await today_events_raw()  # already sorted by start
    for a, b in pairwise(events):
        if a.end > b.start:
            return ProactiveEvent(
                source="calendar_conflict",
                priority=Priority.NOTIFY,
                summary_es=f"Conflicto: {a.title} y {b.title} se traslapan.",
                detail=(
                    f"Garcia, '{a.title}' termina a las {a.end.strftime('%H:%M')} "
                    f"pero '{b.title}' empieza a las {b.start.strftime('%H:%M')}."
                ),
            )
    return None


# ---- C.5 Urgent email from a VIP ------------------------------------------
@polled("urgent_email", 300, "PROACTIVE_URGENT_EMAIL")
async def urgent_email() -> ProactiveEvent | None:
    vips = [s.strip().lower() for s in settings.PROACTIVE_VIP_SENDERS.split(",") if s.strip()]
    if not vips:
        return None
    from tools.mail_tool import list_unread

    rsp = await list_unread(limit=10)
    for line in rsp.user_message.split(";"):
        low = line.lower()
        for vip in vips:
            if vip in low:
                return ProactiveEvent(
                    source="urgent_email",
                    priority=Priority.NOTIFY,
                    summary_es=f"Correo importante: {line.strip()[:140]}",
                    detail=f"Garcia, llegó correo de {vip}: {line.strip()}",
                )
    return None


# ---- C.6 Friday recap ------------------------------------------------------
@scheduled("friday_recap", settings.PROACTIVE_FRIDAY_RECAP_CRON, "PROACTIVE_FRIDAY_RECAP")
async def friday_recap() -> ProactiveEvent | None:
    from memory.long_term import recall

    facts = await recall(limit=12)
    text = "; ".join(f.content for f in facts[:8])
    if not text:
        return None
    return ProactiveEvent(
        source="friday_recap",
        priority=Priority.SPEAK,
        summary_es="Recap del viernes.",
        detail=f"Garcia, recap de la semana: {text}. ¿Quieres profundizar en algo?",
    )


# ---- C.7 Habit tracker (focus/rest nudge) ---------------------------------
@scheduled("habit_tracker", settings.PROACTIVE_HABIT_TRACKER_CRON, "PROACTIVE_HABIT_TRACKER")
async def habit_tracker() -> ProactiveEvent | None:
    return ProactiveEvent(
        source="habit_tracker",
        priority=Priority.AMBIENT,
        summary_es="Pausa sugerida.",
        detail="Garcia, llevas varias horas. ¿Una pausa de 5 minutos?",
    )


# ---- C.8 Memory follow-up --------------------------------------------------
@polled("memory_followup", 1800, "PROACTIVE_MEMORY_FOLLOWUP")
async def memory_followup() -> ProactiveEvent | None:
    from memory.long_term import recall

    facts = await recall("tengo que", limit=5)
    if not facts:
        return None
    f = facts[0]
    return ProactiveEvent(
        source="memory_followup",
        priority=Priority.AMBIENT,
        summary_es=f"Recordatorio: {f.content[:120]}",
        detail=f"Garcia, me dijiste: '{f.content}'. ¿Ya lo hiciste?",
    )


# ---- C.9 Daily intention setting ------------------------------------------
@scheduled("intention_setting", "0 9 * * 1-5", "PROACTIVE_INTENTION_SETTING")
async def intention_setting() -> ProactiveEvent | None:
    return ProactiveEvent(
        source="intention_setting",
        priority=Priority.NOTIFY,
        summary_es="¿Qué quieres lograr hoy?",
        detail="Garcia, ¿qué quieres lograr hoy? Te lo recuerdo al final del día.",
    )


# ---- C.10 Birthday alerts (Contacts.app) ----------------------------------
@scheduled("birthday_alerts", "0 7 * * *", "PROACTIVE_BIRTHDAY_ALERTS")
async def birthday_alerts() -> ProactiveEvent | None:
    from actions import macos

    script = (
        'tell application "Contacts" to get name of every person whose birthday is not '
        "missing value and (month of birthday) = (month of (current date)) and "
        "(day of birthday) = (day of (current date))"
    )
    ok, names = await macos.osascript_or_friendly(script, timeout_s=5.0, on_error="")
    names = names.strip()
    if not ok or not names:
        return None
    return ProactiveEvent(
        source="birthday_alerts",
        priority=Priority.NOTIFY,
        summary_es=f"Cumpleaños hoy: {names}",
        detail=f"Garcia, hoy cumple {names}. ¿Le mando un mensaje?",
    )


# ---- C.11 Overdue reminders -----------------------------------------------
@polled("overdue_reminders", 900, "PROACTIVE_OVERDUE_REMINDERS")
async def overdue_reminders() -> ProactiveEvent | None:
    from actions import macos

    script = (
        'tell application "Reminders" to get name of every reminder whose completed is '
        "false and due date < (current date)"
    )
    ok, text = await macos.osascript_or_friendly(script, timeout_s=5.0, on_error="")
    text = text.strip()
    if not ok or not text:
        return None
    return ProactiveEvent(
        source="overdue_reminders",
        priority=Priority.NOTIFY,
        summary_es=f"Vencidos: {text[:140]}",
        detail=f"Garcia, tienes recordatorios vencidos: {text}",
    )


# ---- C.12 Focus nudge (stub) ----------------------------------------------
@scheduled("focus_nudge", "0 10-19 * * *", "PROACTIVE_FOCUS_NUDGE")
async def focus_nudge() -> ProactiveEvent | None:
    # Real version would track active-app focus time. Placeholder for now.
    return None


# ---- C.13 Background-task completion (15.12 integration) -------------------
async def background_task_subscriber() -> None:
    """Subscribe to the event bus; surface a proactive note when a long task
    finishes. Emitted at AMBIENT (ticker only) because core/background.py
    already sends the macOS notification — we don't double-notify.
    """
    from core import events_bus
    from core.proactive import delivery, engine

    q = events_bus.subscribe()
    log.info("proactive_bg_subscriber_started")
    try:
        while True:
            msg = await q.get()
            if msg.get("type") != "task_completed":
                continue
            if not settings.PROACTIVE_BACKGROUND_TASK_DONE or engine.is_snoozed():
                continue
            name = msg.get("name", "tarea")
            ev = ProactiveEvent(
                source="background_task_done",
                priority=Priority.AMBIENT,
                summary_es=f"Listo: {name}.",
                detail=f"Garcia, terminé '{name}' en {msg.get('elapsed_s', '?')}s.",
                meta=dict(msg),
            )
            await delivery.deliver(ev, ev.priority)
    finally:
        events_bus.unsubscribe(q)
