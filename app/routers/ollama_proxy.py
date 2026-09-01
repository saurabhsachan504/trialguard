"""Authenticated pass-through to the Ollama this server already talks to.

Only the endpoints the extension uses are forwarded. Ollama's management
surface - /api/pull, /api/delete, /api/create, /api/push - is not reachable
here at all, so a stolen token cannot touch what models exist.

Billing is unchanged: the extension already charges through trialguard before
it gets here.

WHY THE RATE LIMIT IS DONE THE WAY IT IS  (this froze the whole app once)
The first version took `db: Session = Depends(get_db)` and called
ratelimit.hit() straight from an `async def`. Two things went wrong together:

  1. A dependency's session is not released until the RESPONSE is finished, and
     these responses stream for a minute. So the INSERT into
     rate_limit_buckets sat in an OPEN transaction for the whole generation.
  2. ratelimit.hit() is synchronous. Called from `async def` it runs ON the
     event loop.

Two summaries started 47 ms apart, both for the same user, so both tried to
insert the same bucket row. Postgres made the second wait for the first - and
that wait happened on the event loop, freezing every request in the process,
including the one that still had to commit the first transaction. Nothing could
ever finish; the site returned Cloudflare 524 until the container was restarted.

So now the limiter runs in a threadpool, in its own short-lived session that
commits and closes BEFORE any streaming starts, with a lock timeout as a last
line of defence. The event loop is never asked to wait on the database.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.services import ratelimit

logger = logging.getLogger("trialguard.ollama")
router = APIRouter(prefix="/ollama", tags=["ollama"])

MAX_BODY_BYTES = 1_000_000
RATE_LIMIT = 60
RATE_WINDOW_SECONDS = 300

# If the bucket row is locked longer than this, waiting will not help.
LOCK_TIMEOUT = "3s"

TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)


def _base() -> str:
    return (settings.OLLAMA_URL or "").rstrip("/")


@contextmanager
def _session() -> Iterator[Session]:
    """A session of our own, independent of the request's dependency session.

    Driving get_db() by hand rather than importing SessionLocal keeps this
    working whatever the session factory is called, and still runs whatever
    teardown get_db() does.
    """
    gen = get_db()
    db = next(gen)
    try:
        yield db
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
        except Exception:
            logger.exception("ollama: session teardown failed")


def _guard_sync(user_id: str) -> None:
    """Count this request, and commit that count immediately.

    Runs in a threadpool. Committing here - instead of leaving it to the end of
    the response - is the whole point: the row lock is held for milliseconds
    rather than for the entire generation.
    """
    with _session() as db:
        try:
            # SET LOCAL, not SET: the setting must die with this transaction,
            # otherwise it rides the pooled connection into unrelated requests.
            db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            ratelimit.hit(
                db,
                f"ollama:user:{user_id}",
                limit=RATE_LIMIT,
                window_seconds=RATE_WINDOW_SECONDS,
            )
            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except SQLAlchemyError:
            db.rollback()
            # The limiter is a guard rail, not the product. If the database
            # cannot answer, serve the user rather than take the site down -
            # which is exactly the failure this file already caused once.
            logger.warning("ollama: rate-limit check skipped", exc_info=True)


async def _guard(user: User) -> None:
    await run_in_threadpool(_guard_sync, str(user.id))


async def _read_body(request: Request) -> bytes:
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Request body too large.",
        )
    if body:
        try:
            json.loads(body)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be JSON."
            )
    return body


async def _forward_stream(path: str, body: bytes) -> AsyncIterator[bytes]:
    """Relay Ollama's response byte-for-byte. A pipe, not a translator."""
    url = f"{_base()}{path}"
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async with client.stream(
            "POST", url, content=body, headers={"Content-Type": "application/json"}
        ) as res:
            if res.status_code != 200:
                detail = (await res.aread())[:400].decode("utf-8", "replace")
                logger.warning("ollama %s -> HTTP %s: %s", path, res.status_code, detail)
                yield json.dumps({"error": f"Ollama returned {res.status_code}"}).encode()
                return
            async for chunk in res.aiter_raw():
                yield chunk


@router.post("/api/chat")
async def chat(request: Request, user: User = Depends(get_current_user)):
    await _guard(user)
    body = await _read_body(request)
    logger.info("ollama chat for user %s (%s bytes)", user.id, len(body))
    return StreamingResponse(
        _forward_stream("/api/chat", body), media_type="application/x-ndjson"
    )


@router.post("/api/generate")
async def generate(request: Request, user: User = Depends(get_current_user)):
    await _guard(user)
    body = await _read_body(request)
    logger.info("ollama generate for user %s (%s bytes)", user.id, len(body))
    return StreamingResponse(
        _forward_stream("/api/generate", body), media_type="application/x-ndjson"
    )


@router.get("/api/tags")
async def tags(user: User = Depends(get_current_user)):
    """The model list, so the options page can populate its dropdowns."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(f"{_base()}/api/tags")
        res.raise_for_status()
        return res.json()
    except Exception as exc:
        logger.warning("ollama tags failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Ollama is not reachable."
        )
