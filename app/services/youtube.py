"""YouTube URL parsing, metadata and transcript retrieval.

The Chrome extension scrapes the transcript inside the user's own browser, which
is fast and never rate-limited because the request comes from a logged-in
residential client. A web app cannot do that (CORS forbids reading youtube.com
from our page), so the fetch happens here instead - and YouTube does throttle
datacenter IPs. The strategy is therefore:

    1. youtube-transcript-api   - fastest, uses the caption tracks directly
    2. yt-dlp                   - slower but a different code path, often works
                                  when (1) is blocked
    3. clear error              - so the UI can say something honest

Set YOUTUBE_PROXY to a residential/rotating proxy if your server's IP gets
blocked; both paths pick it up automatically.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx

from app.config import settings

logger = logging.getLogger("trialguard.youtube")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class TranscriptUnavailable(Exception):
    """No usable transcript could be retrieved."""


@dataclass(slots=True)
class VideoMeta:
    video_id: str
    title: str
    author: str | None
    thumbnail: str
    url: str


@dataclass(slots=True)
class Transcript:
    text: str
    language: str | None
    is_generated: bool
    source: str  # "captions" | "yt-dlp"


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------
def extract_video_id(url_or_id: str) -> str | None:
    """Accept every YouTube URL shape, or a bare 11-character id."""
    value = (url_or_id or "").strip()
    if not value:
        return None
    if _ID_RE.match(value):
        return value

    if "://" not in value:
        value = "https://" + value

    try:
        u = urlparse(value)
    except ValueError:
        return None

    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")

    if host == "youtu.be":
        candidate = u.path.lstrip("/").split("/")[0]
        return candidate if _ID_RE.match(candidate) else None

    if host not in {"youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
        return None

    if u.path == "/watch":
        candidate = (parse_qs(u.query).get("v") or [""])[0]
        return candidate if _ID_RE.match(candidate) else None

    # /shorts/<id>, /embed/<id>, /live/<id>, /v/<id>
    parts = [p for p in u.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
        return parts[1] if _ID_RE.match(parts[1]) else None

    return None


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
async def fetch_metadata(video_id: str) -> VideoMeta:
    """Title/author via the public oEmbed endpoint (no API key needed)."""
    url = watch_url(video_id)
    title, author = f"YouTube video {video_id}", None

    try:
        async with httpx.AsyncClient(timeout=10, proxy=settings.YOUTUBE_PROXY or None) as client:
            res = await client.get(
                "https://www.youtube.com/oembed",
                params={"url": url, "format": "json"},
            )
            if res.status_code == 200:
                data = res.json()
                title = data.get("title") or title
                author = data.get("author_name")
    except Exception as exc:  # pragma: no cover - network
        logger.info("oEmbed lookup failed for %s: %s", video_id, exc)

    return VideoMeta(
        video_id=video_id,
        title=title,
        author=author,
        thumbnail=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        url=url,
    )


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------
def _proxy_config():
    if not settings.YOUTUBE_PROXY:
        return None
    from youtube_transcript_api.proxies import GenericProxyConfig

    return GenericProxyConfig(
        http_url=settings.YOUTUBE_PROXY, https_url=settings.YOUTUBE_PROXY
    )


def _pick_track(transcript_list):
    """Choose the track that is actually in the SPOKEN language.

    A creator can add manual subtitles in a different language (Hindi subs on a
    Punjabi video), so "manual first" picks the wrong one. The auto-generated
    (ASR) track is always the spoken language - use its code to decide, and
    prefer a manual track in that same language when one exists.
    """
    tracks = list(transcript_list)
    if not tracks:
        return None

    asr = next((t for t in tracks if t.is_generated), None)
    if asr is not None:
        spoken = (asr.language_code or "").split("-")[0].lower()
        manual_same = next(
            (
                t
                for t in tracks
                if not t.is_generated
                and (t.language_code or "").split("-")[0].lower() == spoken
            ),
            None,
        )
        return manual_same or asr

    return next((t for t in tracks if not t.is_generated), tracks[0])


def _fetch_via_api(video_id: str) -> Transcript:
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi(proxy_config=_proxy_config())
    listing = api.list(video_id)
    track = _pick_track(listing)
    if track is None:
        raise TranscriptUnavailable("no caption tracks")

    fetched = track.fetch()
    text = " ".join(snippet.text for snippet in fetched)
    return Transcript(
        text=clean_transcript(text),
        language=(track.language_code or "").split("-")[0].lower() or None,
        is_generated=bool(track.is_generated),
        source="captions",
    )


def _parse_json3(payload: str) -> str:
    data = json.loads(payload)
    out = []
    for event in data.get("events", []):
        for seg in event.get("segs", []) or []:
            out.append(seg.get("utf8", ""))
    return "".join(out)


def _parse_vtt(payload: str) -> str:
    lines = []
    for raw in payload.splitlines():
        line = raw.strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if line.isdigit():
            continue
        lines.append(re.sub(r"<[^>]+>", "", line))
    # Auto-captions repeat the previous line as a rolling window; drop repeats.
    deduped = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return " ".join(deduped)


def _fetch_via_ytdlp(video_id: str) -> Transcript:
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "socket_timeout": 20,
    }
    if settings.YOUTUBE_PROXY:
        opts["proxy"] = settings.YOUTUBE_PROXY

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(watch_url(video_id), download=False)

    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    # Same spoken-language logic as above: the auto track defines the language.
    spoken = None
    for code in auto:
        spoken = code.split("-")[0].lower()
        break
    order: list[str] = []
    if spoken:
        order += [c for c in manual if c.split("-")[0].lower() == spoken]
        order += [c for c in auto if c.split("-")[0].lower() == spoken]
    order += list(manual) + list(auto)

    seen: set[str] = set()
    for code in order:
        if code in seen:
            continue
        seen.add(code)
        formats = manual.get(code) or auto.get(code) or []
        chosen = next(
            (f for f in formats if f.get("ext") == "json3"),
            next((f for f in formats if f.get("ext") in ("vtt", "srv1")), None),
        )
        if not chosen or not chosen.get("url"):
            continue
        try:
            with httpx.Client(timeout=25, proxy=settings.YOUTUBE_PROXY or None) as client:
                body = client.get(chosen["url"]).text
            text = _parse_json3(body) if chosen["ext"] == "json3" else _parse_vtt(body)
            text = clean_transcript(text)
            if len(text) > 40:
                return Transcript(
                    text=text,
                    language=code.split("-")[0].lower(),
                    is_generated=code in auto and code not in manual,
                    source="yt-dlp",
                )
        except Exception as exc:  # pragma: no cover - network
            logger.info("yt-dlp subtitle fetch failed (%s): %s", code, exc)

    raise TranscriptUnavailable("yt-dlp found no usable captions")


def fetch_transcript(video_id: str) -> Transcript:
    """Blocking - call it from a worker thread."""
    errors: list[str] = []

    for name, fn in (("captions", _fetch_via_api), ("yt-dlp", _fetch_via_ytdlp)):
        try:
            transcript = fn(video_id)
            if len(transcript.text) > 40:
                logger.info(
                    "transcript for %s via %s (%s chars, lang=%s)",
                    video_id,
                    name,
                    len(transcript.text),
                    transcript.language,
                )
                return transcript
            errors.append(f"{name}: too short")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}".strip()[:200])
            logger.info("transcript %s failed for %s: %s", name, video_id, exc)

    raise TranscriptUnavailable(
        "This video has no usable subtitles, or YouTube is blocking this server. "
        + " | ".join(errors)
    )


# ---------------------------------------------------------------------------
def clean_transcript(text: str) -> str:
    """Strip caption noise like [Music] / [संगीत] / [Applause] and tidy spacing."""
    if not text:
        return ""
    text = re.sub(r"\[[^\]\n]{0,40}\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def sample_for_model(text: str, max_chars: int) -> str:
    """Keep a long transcript within budget without losing the middle/end.

    A few large contiguous blocks, not many tiny fragments - scattered snippets
    break the narrative and the model starts confusing people and events.
    """
    if not text or len(text) <= max_chars:
        return text
    parts = 4
    slice_len = max_chars // parts
    step = len(text) // parts
    return "\n…\n".join(text[i * step : i * step + slice_len].strip() for i in range(parts))
