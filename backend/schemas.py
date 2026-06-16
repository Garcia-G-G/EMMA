"""Pydantic request/response models (Prompt 31)."""

from __future__ import annotations

from pydantic import BaseModel


class SessionStartRequest(BaseModel):
    captcha_token: str = ""


class SessionStartResponse(BaseModel):
    session_token: str
    realtime_url: str
    max_seconds: int


class CheckoutRequest(BaseModel):
    plan: str  # 'pro' | 'team'


class CheckoutResponse(BaseModel):
    url: str


class MeResponse(BaseModel):
    id: int
    email: str
    name: str | None = None
    provider: str
    plan: str
    monthly_session_count: int
    monthly_seconds_used: float
