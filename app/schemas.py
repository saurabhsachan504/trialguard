"""Pydantic request/response models."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------
class DeviceFingerprint(BaseModel):
    """What the extension sends to identify the machine.

    ``installation_id`` is a random UUID the extension persists in
    chrome.storage.local. The remaining fields are stable hardware/browser
    traits, so clearing extension storage alone does not mint a new device.
    ``mac_address`` is only ever populated by an optional native helper.
    """

    installation_id: str = Field(min_length=8, max_length=128)
    platform: str | None = Field(default=None, max_length=64)
    user_agent_brand: str | None = Field(default=None, max_length=200)
    screen: str | None = Field(default=None, max_length=64)
    timezone: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=32)
    hardware_concurrency: int | None = Field(default=None, ge=0, le=1024)
    device_memory: float | None = Field(default=None, ge=0, le=4096)
    gpu: str | None = Field(default=None, max_length=200)
    mac_address: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=120)
    extension_version: str | None = Field(default=None, max_length=32)


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str | None
    platform: str | None
    extension_version: str | None
    revoked: bool
    last_seen_at: datetime
    created_at: datetime


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    full_name: str | None = Field(default=None, max_length=120)
    device: DeviceFingerprint

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class GoogleLoginRequest(BaseModel):
    """Google ka ID token + wahi device fingerprint jo normal signup bhejta hai.

    Fingerprint isi request me aata hai, isliye trial ledgers bilkul waise hi
    bharte hain jaise password wale signup me - koi chhoot nahi.
    """

    credential: str = Field(min_length=20, max_length=8192)
    device: DeviceFingerprint


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)
    device: DeviceFingerprint | None = None

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=256)


class LogoutRequest(BaseModel):
    refresh_token: str | None = None
    all_devices: bool = False


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str | None
    email_verified: bool
    is_active: bool
    trials_used: int
    created_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class AuthResponse(BaseModel):
    user: UserOut
    tokens: TokenPair
    device_id: str
    entitlement: "EntitlementOut"


class EmailRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()


class PasswordResetRequest(BaseModel):
    token: str = Field(min_length=10, max_length=256)
    new_password: str = Field(min_length=1, max_length=200)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=1, max_length=200)


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=10, max_length=256)


class MessageOut(BaseModel):
    detail: str


# ---------------------------------------------------------------------------
# Entitlement / usage
# ---------------------------------------------------------------------------
class EntitlementOut(BaseModel):
    allowed: bool
    reason: str
    plan: Literal["free_trial", "subscription", "blocked"]
    trials_limit: int
    trials_used: int
    trials_remaining: int
    device_trials_used: int
    device_trials_remaining: int
    subscription_status: str | None = None
    current_period_end: datetime | None = None
    upgrade_url: str | None = None


class EntitlementCheckRequest(BaseModel):
    device: DeviceFingerprint


class ConsumeRequest(BaseModel):
    device: DeviceFingerprint
    action: str = Field(default="run", max_length=64)
    idempotency_key: str | None = Field(default=None, max_length=128)
    meta: dict[str, Any] | None = None


class ConsumeResponse(BaseModel):
    allowed: bool
    consumed: bool
    duplicate: bool = False
    granted_by: str
    entitlement: EntitlementOut
    usage_event_id: str | None = None


class UsageEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    action: str
    counted_against_trial: bool
    granted_by: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
class PlanOut(BaseModel):
    id: str
    name: str
    price_cents: int
    currency: str
    interval: str
    description: str
    free_trials: int


class CheckoutRequest(BaseModel):
    success_url: str | None = None
    cancel_url: str | None = None


class CheckoutSessionOut(BaseModel):
    provider: str
    checkout_url: str
    session_id: str
    expires_at: datetime | None = None


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    status: str
    price_cents: int
    currency: str
    current_period_end: datetime | None
    cancel_at_period_end: bool


AuthResponse.model_rebuild()
