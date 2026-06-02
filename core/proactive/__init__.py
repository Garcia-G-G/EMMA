"""Emma's proactive engine.

Turns Emma from purely reactive (wake-word only) into proactive: scheduled
briefings, event-driven alerts, memory follow-ups — each gated by a per-feature
``.env`` flag and demoted/suppressed during quiet hours, DND, and meetings.

Layout:
- ``types``           — ``Priority`` enum + ``ProactiveEvent`` dataclass.
- ``triggers``        — ``@scheduled`` / ``@polled`` registration. Enabled state
                        is read LIVE from ``settings`` so voice toggles take effect.
- ``quiet``           — quiet-hours windows + calendar-aware silence + demotion.
- ``calendar_events`` — shared today's-events fetch (start/end/uid) for the
                        calendar proactivities (the public calendar tool only
                        exposes start+title).
- ``delivery``        — routes a ``ProactiveEvent`` to silent / ambient /
                        notification / spoken based on its effective priority.
- ``voice``           — opens a one-shot Realtime session so Emma speaks unprompted.
- ``engine``          — the tick loop (cron + polling) + background-task subscriber.
- ``proactivities``   — the actual proactivities, registered with the engine.
"""
