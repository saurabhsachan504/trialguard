"""Small DB-backed fixed-window rate limiter.

Good enough for a single-service deployment and it survives restarts / multiple
uvicorn workers. Swap the backend for Redis if you scale horizontally.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.models import RateLimitBucket


def _now() -> datetime:
    return datetime.now(timezone.utc)


def client_ip(request: Request) -> str:
    # Behind Cloudflare, X-Forwarded-For is NOT safe to read from the left:
    # Cloudflare *appends* the real client IP to whatever chain the client
    # sent, so the leftmost entry is attacker-chosen. Anyone could send
    # "X-Forwarded-For: 1.2.3.4" and get a fresh rate-limit bucket per request.
    #
    # CF-Connecting-IP is written by Cloudflare itself and cannot be spoofed,
    # so prefer it. Without it, fall back to the RIGHTMOST X-Forwarded-For
    # entry - the one the closest trusted proxy added.
    if settings.TRUST_PROXY_HEADERS:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()[:64]
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def hit(db: Session, key: str, *, limit: int, window_seconds: int) -> None:
    """Count one request against ``key``; raise 429 when the window is full."""
    if not settings.RATE_LIMIT_ENABLED:
        return

    now = _now()
    window = timedelta(seconds=window_seconds)
    bucket = db.get(RateLimitBucket, key[:200])

    if bucket is None:
        db.add(RateLimitBucket(key=key[:200], window_start=now, count=1))
        db.flush()
        return

    start = bucket.window_start
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    if now - start >= window:
        bucket.window_start = now
        bucket.count = 1
        db.flush()
        return

    if bucket.count >= limit:
        retry_after = int((start + window - now).total_seconds()) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    bucket.count += 1
    db.flush()
