"""Database models.

Trial accounting deliberately uses two ledgers:

* ``User.trials_used``          - how many free runs this *account* consumed.
* ``DeviceTrialLedger.trials_used`` - how many free runs this *machine* consumed,
  summed across every account that has ever run on it.

A free run is only granted when BOTH counters are below the limit, which is what
stops someone from signing up with a second email address to reset the trial.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SubscriptionStatus(str, enum.Enum):
    incomplete = "incomplete"
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    canceled = "canceled"
    unpaid = "unpaid"

    @property
    def grants_access(self) -> bool:
        return self in (SubscriptionStatus.active, SubscriptionStatus.trialing)


class TokenPurpose(str, enum.Enum):
    email_verify = "email_verify"
    password_reset = "password_reset"


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Always stored lower-cased; uniqueness is therefore case-insensitive.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Google ki sthaayi user id. Google se aane walon ke liye bharti hai,
    # baaki sabke liye NULL - purane rows ko koi farak nahi padta.
    google_sub: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True
    )
    # "password" | "google" | "google+password" - sirf jaankari ke liye.
    auth_provider: Mapped[str | None] = mapped_column(String(24), default="password")
    full_name: Mapped[str | None] = mapped_column(String(120))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    trials_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Optional per-account override of the global free limit (support/grants).
    trial_limit_override: Mapped[int | None] = mapped_column(Integer)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signup_ip: Mapped[str | None] = mapped_column(String(64))

    devices: Mapped[list["Device"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    usage_events: Mapped[list["UsageEvent"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def active_subscription(self) -> "Subscription | None":
        for sub in self.subscriptions:
            if SubscriptionStatus(sub.status).grants_access:
                return sub
        return None


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
class DeviceTrialLedger(TimestampMixin, Base):
    """Global, account-independent trial counter for one physical device."""

    __tablename__ = "device_trial_ledger"

    device_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    trials_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    account_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    block_reason: Mapped[str | None] = mapped_column(String(255))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Device(TimestampMixin, Base):
    """A device as seen by one particular account."""

    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("user_id", "device_hash", name="uq_device_user_hash"),
        Index("ix_devices_hash", "device_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # HMAC-SHA256 of the raw fingerprint; the raw value is never persisted.
    device_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    label: Mapped[str | None] = mapped_column(String(120))
    platform: Mapped[str | None] = mapped_column(String(64))
    extension_version: Mapped[str | None] = mapped_column(String(32))
    # Set only when a native helper reported a real hardware MAC address.
    mac_address_hash: Mapped[str | None] = mapped_column(String(64))

    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_ip: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="devices")


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_usage_idempotency"),
        Index("ix_usage_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL")
    )
    device_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    action: Mapped[str] = mapped_column(String(64), default="run", nullable=False)
    # True when this event decremented the free-trial allowance.
    counted_against_trial: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # "trial" | "subscription" | "override"
    granted_by: Mapped[str] = mapped_column(String(32), default="trial", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    meta: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="usage_events")


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_subscription_id", name="uq_provider_subscription"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_customer_id: Mapped[str | None] = mapped_column(String(128), index=True)
    provider_subscription_id: Mapped[str | None] = mapped_column(String(128))

    status: Mapped[str] = mapped_column(
        Enum(SubscriptionStatus, native_enum=False, length=32),
        default=SubscriptionStatus.incomplete,
        nullable=False,
    )
    price_cents: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)

    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="subscriptions")


class WebhookEvent(Base):
    """Records processed webhook ids so retries are idempotent."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_webhook_provider_event"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Auth tokens
# ---------------------------------------------------------------------------
class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    device_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def is_valid(self) -> bool:
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return self.revoked_at is None and exp > utcnow()


class OneTimeToken(Base):
    """Email-verification and password-reset tokens (stored hashed)."""

    __tablename__ = "one_time_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(
        Enum(TokenPurpose, native_enum=False, length=32), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def is_valid(self) -> bool:
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return self.used_at is None and exp > utcnow()


class RateLimitBucket(Base):
    """Tiny DB-backed fixed-window rate limiter (works across workers)."""

    __tablename__ = "rate_limit_buckets"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class CachedOutput(Base):
    """One generated summary, reusable by everyone who asks for the same thing.

    The same video is summarised by many different people and the answer is the
    same every time. Generating it costs minutes of GPU; reading it back is one
    indexed SELECT. So the first person pays for it and everyone after is served
    instantly - they are still charged a trial, because they are still getting
    the product.

    The unique constraint covers everything that changes the output. Two of
    those are easy to forget:
      * ``model`` - a summary written by gemma2 is not the one gemma4 writes.
      * ``prompt_version`` - bumped by hand in output_cache.py whenever a prompt
        changes, so improving a prompt cannot leave people reading the old
        wording forever.
    """

    __tablename__ = "cached_outputs"
    __table_args__ = (
        UniqueConstraint(
            "video_id", "mode", "lang", "model", "prompt_version",
            name="uq_cached_output_key",
        ),
        # purge_stale() sweeps by age; without this it would scan the table.
        Index("ix_cached_outputs_last_used_at", "last_used_at"),
        # detected_lang_for() looks up a video without knowing the rest of the key.
        Index("ix_cached_outputs_video_id", "video_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    video_id: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    # The language the OUTPUT is written in - not the video's own language.
    lang: Mapped[str] = mapped_column(String(8), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # The video's OWN language. Lets a "same as the video" request be answered
    # from cache without fetching the transcript first - and fetching the
    # transcript is the slow, rate-limited part.
    detected_lang: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    transcript_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
