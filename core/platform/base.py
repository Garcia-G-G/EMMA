"""Shared platform-layer primitives (Prompt 30)."""

from __future__ import annotations


class UnsupportedOnPlatform(RuntimeError):  # noqa: N818 — deliberate API name (caught by tools)
    """A capability with no implementation on the current OS yet.

    Raised by Windows/stub impls of Tier-2 capabilities. Tools catch it at their
    boundary and turn it into a friendly Spanish line ("en Windows no tengo eso
    todavía"), never a stack trace to the user.
    """

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(f"{capability} no está disponible en este sistema todavía")
