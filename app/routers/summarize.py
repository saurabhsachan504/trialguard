"""YouTube summarisation endpoints for the web app.

Billing rule, identical to the extension: **one trial = one video**. The
idempotency key sent to the entitlement engine is `video:<youtube_id>`, so the
summary, the key points and the full PDF notes for a single video all share one
charge, and re-summarising a video you already paid for is free forever.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import DeviceFingerprint, EntitlementOut
from app.services import entitlements, output_cache, ratelimit, summarizer, translate, youtube

logger = logging.getLogger("trialguard.summarize")
router = APIRouter(tags=["summarize"])


# ---------------------------------------------------------------------------
class VideoRequest(BaseModel):
    url: str = Field(min_length=5, max_length=500)
    device: DeviceFingerprint
    # None / "auto" => write in the video's own language.
    target_lang: str | None = Field(default=None, max_length=8)

    # The extension (and, later, the mobile app) reads the transcript inside
    # the user's own browser, where YouTube sees an ordinary viewer instead of
    # our server. When that text arrives we never touch YouTube at all - which
    # is why that path can never be rate-limited or IP-blocked.
    #
    # The cap is a memory guard, not a trust boundary: a caller who sends
    # nonsense only wastes their own trial, because billing happens before
    # this text is ever read.
    transcript: str | None = Field(default=None, max_length=200_000)
    transcript_lang: str | None = Field(default=None, max_length=8)


class SummarizeRequest(VideoRequest):
    mode: str = Field(default="summary", pattern="^(summary|key_points|notes)$")


class TranslateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    target_lang: str = Field(min_length=2, max_length=8)


class TranslateOut(BaseModel):
    text: str
    target_lang: str
    language_name: str


def _resolve_target(requested: str | None, detected: str) -> str:
    """The language the user actually gets."""
    if not requested or requested in ("auto", "same"):
        return detected
    return requested.split("-")[0].lower()


# Long enough to be a real transcript rather than an empty string or a stray
# word - the same threshold fetch_transcript() uses on its own results.
_MIN_CLIENT_TRANSCRIPT = 40


async def _obtain_transcript(payload: VideoRequest, video_id: str) -> youtube.Transcript:
    """Use the client's transcript when it sent one; otherwise fetch it here.

    Falling back matters: a visitor without the extension, or on a phone, still
    gets the old server-side path, so nothing that works today stops working.
    """
    supplied = (payload.transcript or "").strip()
    if len(supplied) >= _MIN_CLIENT_TRANSCRIPT:
        logger.info(
            "transcript supplied by client for %s (%s chars, lang=%s)",
            video_id,
            len(supplied),
            payload.transcript_lang,
        )
        return youtube.Transcript(
            text=youtube.clean_transcript(supplied),
            language=(payload.transcript_lang or "").split("-")[0].lower() or None,
            is_generated=True,
            source="client",
        )

    return await run_in_threadpool(youtube.fetch_transcript, video_id)


class VideoInfoOut(BaseModel):
    video_id: str
    title: str
    author: str | None
    thumbnail: str
    url: str


def _video_id_or_400(url: str) -> str:
    video_id = youtube.extract_video_id(url)
    if not video_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That doesn't look like a YouTube link. Paste a youtube.com or youtu.be URL.",
        )
    return video_id


def _event(payload: dict) -> bytes:
    """One NDJSON line - the browser reads these as they arrive."""
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
@router.post("/video/info", response_model=VideoInfoOut)
async def video_info(payload: VideoRequest, user: User = Depends(get_current_user)):
    """Title + thumbnail so the UI can show the video immediately.

    Deliberately free: it spends no trial, because nothing has been generated.
    """
    video_id = _video_id_or_400(payload.url)
    meta = await youtube.fetch_metadata(video_id)
    return VideoInfoOut(**dataclasses.asdict(meta))


def _store_cached(
    video_id: str,
    mode: str,
    target: str,
    model: str,
    text: str,
    detected: str,
    transcript_chars: int,
) -> None:
    """Cache me likho - APNI ALAG session me, threadpool par.

    Request ki apni session streaming ke poore samay khuli rehti hai. Usi par
    likhna do tarah se khatarnak hai, aur dono baar hum ye dekh chuke hain:

      * likhna fail ho jaye to session poisoned ho jaati hai (PendingRollback),
        aur uske baad us session se kuch bhi nahi ho paata.
      * lamba chalne wale transaction me row lock pakde rehna - isi ne poore
        app ko jama diya tha (Cloudflare 524).

    Isliye yahan apni chhoti session kholi jaati hai, likhkar turant band. Jo
    bhi bigde, wo yahin ruk jaata hai - user ka jawab to ja hi chuka hai.
    """
    gen = get_db()
    db = next(gen)
    try:
        output_cache.put(
            db,
            video_id,
            mode,
            target,
            model,
            text,
            detected_lang=detected,
            transcript_chars=transcript_chars,
        )
    except Exception:  # noqa: BLE001 - cache kabhi summary na todhe
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("cache me rakhna fail hua: %s", video_id, exc_info=True)
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
        except Exception:
            logger.exception("cache session band karne me dikkat")


def _cache_lookup(db: Session, payload, video_id: str):
    """(row, target, model). row None hai to cache me kuch nahi mila.

    Ye transcript laane se PEHLE chalta hai, isliye hit hone par YouTube par ek
    bhi request nahi jaati - aur wahi requests pehle rate-limit ka kaaran bani
    thi.

    Agar user ne bhasha nahi chuni ("video ki apni bhasha"), to hamein pata
    nahi ki wo kaun si hai - wo transcript se nikalti hai. Isliye cache se hi
    poocha jaata hai ki is video ki bhasha pehle kya nikli thi.
    """
    requested = (payload.target_lang or "").strip().lower()
    if requested in ("", "auto", "same"):
        detected = output_cache.detected_lang_for(db, video_id)
        if not detected:
            # Is video ko pehle kabhi nahi dekha - to cache me ho hi nahi sakti.
            return None, "", ""
    else:
        detected = ""
    # Wahi function jo saamanya raaste me chalta hai, taaki dono kabhi alag na
    # ho jayein.
    target = _resolve_target(payload.target_lang, detected)
    model = summarizer.plan_for(target)[0]
    return output_cache.get(db, video_id, payload.mode, target, model), target, model


async def _replay_cached(row, *, meta, target: str, model: str, entitlement):
    """Cache se mili summary ko usi shakl me bhejo jaisi taazi banti hai.

    Ek hi delta me poora text - saamanya raaste jaisa hi kram (meta, delta,
    done), taaki UI me kuch alag na karna pade.
    """
    yield _event(
        {
            "type": "meta",
            "video": dataclasses.asdict(meta),
            "detected_language": row.detected_lang or target,
            "detected_language_name": summarizer.language_name(row.detected_lang or target),
            "language": target,
            "language_name": summarizer.language_name(target),
            "model": model,
            "transcript_chars": row.transcript_chars,
            "transcript_source": "cache",
            "cached": True,
            "entitlement": json.loads(entitlement.model_dump_json()),
        }
    )
    yield _event({"type": "delta", "text": row.text})
    yield _event({"type": "done", "text": row.text, "language": target, "cached": True})


@router.post("/summarize")
async def summarize(
    payload: SummarizeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream a summary in the video's own language.

    Response is NDJSON, one JSON object per line:
      {"type":"meta",  "video": {...}, "language": "hi", "entitlement": {...}}
      {"type":"delta", "text": "..."}          (many)
      {"type":"done",  "text": "<full markdown>"}
      {"type":"error", "message": "..."}
    """
    video_id = _video_id_or_400(payload.url)

    # 1. Charge (or confirm we already charged for this video).
    result = entitlements.consume(
        db,
        user,
        payload.device,
        action=f"summarize:{payload.mode}",
        idempotency_key=f"video:{video_id}",
        meta={"video_id": video_id, "mode": payload.mode, "surface": "web"},
    )
    db.commit()
    entitlement: EntitlementOut = result.entitlement

    # 2. Metadata + transcript. Both happen BEFORE the stream opens so a
    #    failure here is a normal HTTP error the UI can show cleanly.
    meta = await youtube.fetch_metadata(video_id)

    # 2a. Cache. Wahi video, wahi mode, wahi bhasha, wahi model = wahi jawab.
    #     Ye transcript laane se pehle hai, isliye hit par YouTube ko chhua bhi
    #     nahi jaata. (Cache band ho to ye chupchap None lautata hai.)
    cached_row, cached_target, cached_model = _cache_lookup(db, payload, video_id)
    if cached_row is not None:
        return StreamingResponse(
            _replay_cached(
                cached_row,
                meta=meta,
                target=cached_target,
                model=cached_model,
                entitlement=entitlement,
            ),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    try:
        transcript = await _obtain_transcript(payload, video_id)
    except youtube.TranscriptUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    detected = summarizer.detect_language(transcript.text, hint=transcript.language)
    target = _resolve_target(payload.target_lang, detected)
    model, write_lang, translate_to = summarizer.plan_for(target)

    async def generate() -> AsyncIterator[bytes]:
        yield _event(
            {
                "type": "meta",
                "video": dataclasses.asdict(meta),
                "detected_language": detected,
                "detected_language_name": summarizer.language_name(detected),
                "language": target,
                "language_name": summarizer.language_name(target),
                "model": model,
                "transcript_chars": len(transcript.text),
                "transcript_source": transcript.source,
                "entitlement": json.loads(entitlement.model_dump_json()),
            }
        )

        collected: list[str] = []
        try:
            async for delta in summarizer.stream_summary(
                transcript.text, lang=write_lang, mode=payload.mode
            ):
                collected.append(delta)
                yield _event({"type": "delta", "text": delta})
        except Exception as exc:  # noqa: BLE001 - surface it to the UI
            logger.exception("summary failed for %s", video_id)
            if collected:
                # Partial output is still useful - hand it over rather than
                # throwing away what the model already wrote.
                yield _event({"type": "done", "text": "".join(collected), "partial": True})
            else:
                yield _event({"type": "error", "message": _friendly(exc)})
            return

        text = "".join(collected)

        # Two ways the text can end up in the wrong language: we deliberately
        # wrote English for a language the model handles badly, or the model
        # simply ignored the instruction. Both are fixed the same way.
        if translate_to:
            yield _event({"type": "status", "message": f"Translating to {summarizer.language_name(translate_to)}…"})
            try:
                text = await translate.translate(text, translate_to)
            except Exception:  # pragma: no cover - network
                logger.warning("translation to %s failed", translate_to)
        else:
            text, fixed = await translate.ensure_language(text, target)
            if fixed:
                yield _event(
                    {
                        "type": "status",
                        "message": f"The model answered in the wrong language - translated to {summarizer.language_name(target)}.",
                    }
                )

        yield _event({"type": "done", "text": text, "language": target})

        # Jama karna SABSE AAKHIR me - user ka jawab ja chuka hai, to yahan
        # kuch bigde bhi to farq nahi padta. Cache band ho to _store_cached()
        # khud hi kuch nahi karta.
        if youtube.is_cacheable(transcript):
            await run_in_threadpool(
                _store_cached,
                video_id,
                payload.mode,
                target,
                model,
                text,
                detected,
                len(transcript.text),
            )

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/notes")
async def notes(
    payload: VideoRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full, chunked notes covering the whole video - what the PDF is built from.

    Same idempotency key as /summarize, so a user who already summarised this
    video is not charged again for the PDF.
    """
    video_id = _video_id_or_400(payload.url)

    entitlements.consume(
        db,
        user,
        payload.device,
        action="notes",
        idempotency_key=f"video:{video_id}",
        meta={"video_id": video_id, "mode": "notes", "surface": "web"},
    )
    db.commit()

    meta = await youtube.fetch_metadata(video_id)
    try:
        transcript = await _obtain_transcript(payload, video_id)
    except youtube.TranscriptUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    detected = summarizer.detect_language(transcript.text, hint=transcript.language)
    target = _resolve_target(payload.target_lang, detected)
    _model, lang, translate_to = summarizer.plan_for(target)

    async def generate() -> AsyncIterator[bytes]:
        queue: list[bytes] = []

        async def on_progress(done: int, total: int) -> None:
            queue.append(
                _event(
                    {
                        "type": "progress",
                        "done": done,
                        "total": total,
                        "percent": round(done / total * 100),
                    }
                )
            )

        async def on_warning(message: str) -> None:
            # Anything that could make the notes incomplete is surfaced, never
            # swallowed - the whole promise of this feature is completeness.
            queue.append(_event({"type": "warning", "message": message}))

        yield _event(
            {
                "type": "meta",
                "video": dataclasses.asdict(meta),
                "detected_language": detected,
                "detected_language_name": summarizer.language_name(detected),
                "language": target,
                "language_name": summarizer.language_name(target),
            }
        )

        try:
            # full_notes reports progress through the callback; we drain the
            # queue between chunks so the browser sees a live progress bar.
            import asyncio

            task = asyncio.create_task(
                summarizer.full_notes(
                    transcript.text,
                    lang=lang,
                    on_progress=on_progress,
                    on_warning=on_warning,
                )
            )
            while not task.done():
                await asyncio.sleep(0.4)
                while queue:
                    yield queue.pop(0)
            while queue:
                yield queue.pop(0)
            text = await task
        except Exception as exc:  # noqa: BLE001
            logger.exception("notes failed for %s", video_id)
            yield _event({"type": "error", "message": _friendly(exc)})
            return

        if not text:
            yield _event({"type": "error", "message": "The model returned nothing. Please try again."})
            return

        if translate_to:
            yield _event({"type": "status", "message": f"Translating to {summarizer.language_name(translate_to)}…"})
            try:
                text = await translate.translate(text, translate_to)
            except Exception:  # pragma: no cover - network
                logger.warning("notes translation to %s failed", translate_to)
        else:
            text, _fixed = await translate.ensure_language(text, target)

        yield _event({"type": "done", "text": text, "language": target})

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/translate", response_model=TranslateOut)
async def translate_text(
    payload: TranslateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Translate an already-generated summary or notes into another language.

    No trial is charged: the video was already paid for, this is just a
    presentation of the same result. Rate-limited so it cannot be used as a free
    general-purpose translation API.
    """
    ratelimit.hit(db, f"translate:{user.id}", limit=60, window_seconds=3600)
    db.commit()

    target = payload.target_lang.split("-")[0].lower()
    try:
        text = await translate.translate(payload.text, target)
    except Exception as exc:  # noqa: BLE001
        logger.warning("translate failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Translation service is unreachable right now. Please try again.",
        )
    return TranslateOut(
        text=text, target_lang=target, language_name=summarizer.language_name(target)
    )


def _friendly(exc: Exception) -> str:
    text = str(exc)
    if "Ollama HTTP 404" in text:
        return (
            f"The model '{settings.OLLAMA_MODEL}' is not installed on your Ollama server."
        )
    if "ConnectError" in type(exc).__name__ or "Connect" in text:
        return "Can't reach the AI server. Check OLLAMA_URL and that Ollama is running."
    if "Timeout" in type(exc).__name__ or "timeout" in text.lower():
        return "The AI server took too long to answer. Try a shorter video."
    return f"Summary failed: {text[:200]}"
