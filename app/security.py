"""Password hashing, JWT issuing/verification and device fingerprint hashing."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(
    schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=settings.BCRYPT_ROUNDS
)

_COMMON_PASSWORDS = {
    "password", "12345678", "123456789", "qwerty123", "password1",
    "11111111", "iloveyou", "admin123", "welcome1", "letmein1",
}


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    # bcrypt silently truncates beyond 72 bytes; pre-hash so long passwords
    # keep their full entropy.
    return pwd_context.hash(_prehash(password))


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(_prehash(password), password_hash)
    except ValueError:
        return False


def _prehash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def validate_password_strength(password: str) -> list[str]:
    """Return a list of problems; empty list means the password is acceptable."""
    problems: list[str] = []
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        problems.append(
            f"must be at least {settings.PASSWORD_MIN_LENGTH} characters long"
        )
    if len(password) > 200:
        problems.append("must be at most 200 characters long")
    if not re.search(r"[A-Za-z]", password):
        problems.append("must contain at least one letter")
    if not re.search(r"\d", password):
        problems.append("must contain at least one digit")
    if password.lower() in _COMMON_PASSWORDS:
        problems.append("is too common")
    return problems


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(
    user_id: str, *, extra_claims: dict[str, Any] | None = None
) -> tuple[str, int]:
    """Return (token, expires_in_seconds)."""
    ttl = timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES)
    now = _now()
    payload: dict[str, Any] = {
        "sub": user_id,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": secrets.token_urlsafe(12),
        "iss": settings.APP_NAME,
    }
    if extra_claims:
        payload.update(extra_claims)
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError on any problem."""
    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
        options={"require": ["exp", "sub"]},
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def generate_refresh_token() -> tuple[str, str, datetime]:
    """Return (raw_token, token_hash, expires_at). Only the hash is stored."""
    raw = secrets.token_urlsafe(48)
    return raw, sha256(raw), _now() + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)


def generate_one_time_token(ttl: timedelta) -> tuple[str, str, datetime]:
    raw = secrets.token_urlsafe(32)
    return raw, sha256(raw), _now() + ttl


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Device fingerprints
# ---------------------------------------------------------------------------
# A Chrome extension cannot read a MAC address - the browser sandbox exposes no
# network-hardware API. We therefore accept a fingerprint assembled by the
# extension (a persisted random installation id plus stable hardware/browser
# traits) and, optionally, a real MAC reported by a native messaging helper.
# Whatever we receive is HMAC'd with a server-side pepper before storage, so a
# database leak cannot be replayed or reversed into device identifiers.
FINGERPRINT_FIELDS = (
    "installation_id",
    "platform",
    "user_agent_brand",
    "screen",
    "timezone",
    "language",
    "hardware_concurrency",
    "device_memory",
    "gpu",
    "mac_address",
)


# Subset used for the coarse machine ledger. Deliberately excludes anything the
# client can regenerate at will (installation_id) and anything that drifts
# (browser version, timezone while travelling, UI language).
MACHINE_FIELDS = (
    "platform",
    "screen",
    "hardware_concurrency",
    "device_memory",
    "gpu",
)


def canonical_fingerprint(fingerprint: dict[str, Any]) -> str:
    """Stable string form of a fingerprint, ignoring unknown/empty fields."""
    cleaned = {
        k: str(fingerprint.get(k)).strip().lower()
        for k in FINGERPRINT_FIELDS
        if fingerprint.get(k) not in (None, "")
    }
    if not cleaned:
        raise ValueError("empty device fingerprint")
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"))


def hash_device(fingerprint: dict[str, Any] | str) -> str:
    """HMAC-SHA256 a fingerprint (dict or opaque client-computed string)."""
    material = (
        fingerprint if isinstance(fingerprint, str) else canonical_fingerprint(fingerprint)
    )
    return hmac.new(
        settings.DEVICE_HASH_SECRET.encode("utf-8"),
        material.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def hash_machine(fingerprint: dict[str, Any]) -> str | None:
    """Hash only the stable hardware traits. None when there is too little to go on."""
    cleaned = {
        k: str(fingerprint.get(k)).strip().lower()
        for k in MACHINE_FIELDS
        if fingerprint.get(k) not in (None, "")
    }
    # Two or fewer traits is not enough to distinguish machines; refuse rather
    # than lump unrelated users into one ledger row.
    if len(cleaned) < 3:
        return None
    material = "machine:" + json.dumps(cleaned, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        settings.DEVICE_HASH_SECRET.encode("utf-8"),
        material.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def hash_mac_address(mac: str) -> str:
    normalised = re.sub(r"[^0-9a-f]", "", mac.lower())
    return hmac.new(
        settings.DEVICE_HASH_SECRET.encode("utf-8"),
        f"mac:{normalised}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
