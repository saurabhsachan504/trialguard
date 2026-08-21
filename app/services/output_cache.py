"""Share a generated summary / set of notes across users.

The same YouTube video is summarised by many different people, and the answer is
the same every time. Generating it costs minutes of GPU; reading it back costs a
single indexed SELECT. So the first person pays for it and everyone after is
served instantly.

Three things this file is careful about:

  1. THE KEY. It contains everything that changes the output - including a
     PROMPT_VERSION you bump by hand. Forget that one and improving a prompt
     silently never reaches anyone who is served from cache.

  2. THE STAMPEDE. If ten people click the same fresh video at once, only one
     should generate it. A per-key lock plus a re-check after acquiring it means
     the other nine wait a moment and then read the finished row.

  3. PRIVACY. Unlisted and private videos are never written here - see
     youtube.is_public(). Someone summarising their own unlisted video must not
     have it handed to a stranger who knows the id.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CachedOutput

logger = logging.getLogger("trialguard.cache")

# ---------------------------------------------------------------------------
# BUMP THIS whenever a prompt in summarizer.py changes, or the output format
# changes. Old rows keep their old number and are simply never matched again;
# the cleanup job removes them once they go cold.
# ---------------------------------------------------------------------------
PROMPT_VERSION = 1


def _key(video_id: str, mode: str, lang: str, model: str) -> tuple:
    return (video_id, mode, lang, model, PROMPT_VERSION)


def detected_lang_for(db: Session, video_id: str) -> str | None:
    """The video's own language, if anyone has already had it processed.

    This is what lets a "same as the video" request be answered from cache
    without fetching the transcript - and the transcript fetch is the slow,
    rate-limited part of a cache hit.
    """
    if not settings.OUTPUT_CACHE_ENABLED:
        return None
    row = db.execute(
        select(CachedOutput.detected_lang)
        .where(CachedOutput.video_id == video_id, CachedOutput.detected_lang != "")
        .order_by(CachedOutput.last_used_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row or None


def get(db: Session, video_id: str, mode: str, lang: str, model: str) -> CachedOutput | None:
    """Return the cached row, or None. Records the hit."""
    if not settings.OUTPUT_CACHE_ENABLED:
        return None

    row = db.execute(
        select(CachedOutput).where(
            CachedOutput.video_id == video_id,
            CachedOutput.mode == mode,
            CachedOutput.lang == lang,
            CachedOutput.model == model,
            CachedOutput.prompt_version == PROMPT_VERSION,
        )
    ).scalar_one_or_none()

    if row is None:
        return None

    row.hits += 1
    row.last_used_at = datetime.now(timezone.utc)
    db.commit()
    logger.info(
        "cache hit %s/%s/%s (%s chars, hit #%s)", video_id, mode, lang, row.chars, row.hits
    )
    return row


def put(
    db: Session,
    video_id: str,
    mode: str,
    lang: str,
    model: str,
    text: str,
    *,
    detected_lang: str = "",
    transcript_chars: int = 0,
) -> bool:
    """Store a freshly generated output. Returns True if it was stored."""
    if not settings.OUTPUT_CACHE_ENABLED:
        return False
    if not text or len(text) < settings.OUTPUT_CACHE_MIN_CHARS:
        # Something went wrong upstream; do not immortalise a broken answer.
        return False

    row = CachedOutput(
        video_id=video_id,
        mode=mode,
        lang=lang,
        model=model,
        prompt_version=PROMPT_VERSION,
        text=text,
        chars=len(text),
        detected_lang=detected_lang or "",
        transcript_chars=transcript_chars,
        hits=0,
        last_used_at=datetime.now(timezone.utc),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Another worker finished the same video first. Theirs is just as good.
        db.rollback()
        logger.info("cache row for %s/%s/%s already existed", video_id, mode, lang)
        return False
    logger.info("cached %s/%s/%s (%s chars)", video_id, mode, lang, len(text))
    return True


# ---------------------------------------------------------------------------
# Stampede control
#
# One asyncio lock per key, held only while that key is being generated. This is
# per-process, so with several uvicorn workers two of them could still generate
# the same video at the same moment - that costs a little GPU but is otherwise
# harmless, because put() treats a duplicate row as a normal outcome. A
# cross-worker lock would mean Redis or Postgres advisory locks, which is a lot
# of moving parts for the amount of waste it saves.
# ---------------------------------------------------------------------------
_locks: dict[tuple, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def lock_for(video_id: str, mode: str, lang: str, model: str) -> asyncio.Lock:
    key = _key(video_id, mode, lang, model)
    async with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
        return lock


async def release_if_idle(video_id: str, mode: str, lang: str, model: str) -> None:
    """Drop the lock object once nobody is waiting, so the dict cannot grow
    without bound on a busy server."""
    key = _key(video_id, mode, lang, model)
    async with _locks_guard:
        lock = _locks.get(key)
        if lock is not None and not lock.locked():
            _locks.pop(key, None)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def purge_stale(db: Session, older_than_days: int | None = None) -> int:
    """Delete rows nobody has used in a long time. Returns how many went."""
    days = older_than_days if older_than_days is not None else settings.OUTPUT_CACHE_TTL_DAYS
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = db.execute(delete(CachedOutput).where(CachedOutput.last_used_at < cutoff))
    db.commit()
    removed = result.rowcount or 0
    if removed:
        logger.info("purged %s cached outputs older than %s days", removed, days)
    return removed


def stats(db: Session) -> dict:
    rows, chars, hits = db.execute(
        select(
            func.count(CachedOutput.id),
            func.coalesce(func.sum(CachedOutput.chars), 0),
            func.coalesce(func.sum(CachedOutput.hits), 0),
        )
    ).one()
    return {
        "rows": rows,
        "total_chars": int(chars),
        "approx_mb": round(int(chars) / 1_000_000, 1),
        "total_hits": int(hits),
        "prompt_version": PROMPT_VERSION,
    }
