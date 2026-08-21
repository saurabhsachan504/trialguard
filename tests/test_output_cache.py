"""The shared output cache: one person generates, everyone else reads.

Every test here was checked by breaking the thing it covers and watching it fail.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.config import settings
from app.models import CachedOutput
from app.services import output_cache, summarizer, youtube
from tests.conftest import register

API = settings.API_PREFIX

HINDI = "यह वीडियो भारत के इतिहास के बारे में है। " * 40
VIDEO = "https://youtu.be/dQw4w9WgXcQ"
VIDEO_ID = "dQw4w9WgXcQ"
OTHER = "https://youtu.be/aaaaaaaaaaa"


@pytest.fixture
def cache_on(monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "OUTPUT_CACHE_MIN_CHARS", 5)
    # The public/unlisted check is a real network call; tests say "public"
    # unless a test overrides it.
    async def public(video_id):
        return True

    monkeypatch.setattr("app.routers.summarize.youtube.is_public", public)
    yield


@pytest.fixture
def stub_youtube(monkeypatch):
    calls = {"meta": 0, "transcript": 0}

    async def fake_meta(video_id):
        calls["meta"] += 1
        return youtube.VideoMeta(
            video_id=video_id,
            title="Test video",
            author="Test channel",
            thumbnail="",
            url=youtube.watch_url(video_id),
        )

    def fake_transcript(video_id):
        calls["transcript"] += 1
        return youtube.Transcript(
            text=HINDI, language="hi", is_generated=True, source="captions"
        )

    monkeypatch.setattr("app.routers.summarize.youtube.fetch_metadata", fake_meta)
    monkeypatch.setattr("app.routers.summarize.youtube.fetch_transcript", fake_transcript)
    return calls


@pytest.fixture
def stub_model(monkeypatch):
    calls = {"n": 0}

    async def fake_stream(**kwargs):
        calls["n"] += 1
        for piece in ["## अवलोकन\n", "यह एक ", "परीक्षण सारांश है जो काफी लंबा है।"]:
            yield piece

    monkeypatch.setattr(summarizer, "stream_chat", fake_stream)
    return calls


def events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def summarize(client, headers, device, url=VIDEO, lang=None):
    body = {"url": url, "device": device, "mode": "summary"}
    if lang:
        body["target_lang"] = lang
    return client.post(f"{API}/summarize", json=body, headers=headers)


# ---------------------------------------------------------------------------
def test_second_person_is_served_from_cache(
    client, device, cache_on, stub_youtube, stub_model, db
):
    """The whole point: user 2 gets it without the model or YouTube."""
    _, h1, d1 = register(client, email="one@example.com", device=device)
    first = summarize(client, h1, d1)
    assert first.status_code == 200, first.text
    assert stub_model["n"] == 1
    assert db.query(CachedOutput).count() == 1

    from tests.conftest import make_device

    d2 = make_device(installation_id="second-person", gpu="AMD Radeon")
    _, h2, _ = register(client, email="two@example.com", device=d2)

    transcripts_before = stub_youtube["transcript"]
    second = summarize(client, h2, d2)
    assert second.status_code == 200, second.text

    evs = events(second)
    assert evs[0]["cached"] is True
    assert evs[-1]["type"] == "done"
    assert "परीक्षण सारांश" in evs[-1]["text"]

    # Neither the model nor YouTube's transcript endpoint was touched again.
    assert stub_model["n"] == 1, "the model ran a second time"
    assert stub_youtube["transcript"] == transcripts_before, "transcript was re-fetched"


def test_a_cache_hit_still_costs_a_credit(
    client, device, cache_on, stub_youtube, stub_model
):
    """Decided deliberately: the value to the user is the same either way."""
    from tests.conftest import make_device

    _, h1, d1 = register(client, email="one@example.com", device=device)
    summarize(client, h1, d1)

    d2 = make_device(installation_id="charged-person")
    _, h2, _ = register(client, email="two@example.com", device=d2)
    res = summarize(client, h2, d2)

    meta = events(res)[0]
    assert meta["entitlement"]["trials_remaining"] == 4, "cached answer was given away free"


def test_the_same_person_twice_is_still_charged_once(
    client, device, cache_on, stub_youtube, stub_model
):
    """Idempotency must survive caching - the second click is the same video."""
    _, headers, d = register(client, device=device)
    summarize(client, headers, d)
    second = summarize(client, headers, d)
    assert events(second)[0]["entitlement"]["trials_remaining"] == 4


def test_a_different_language_is_a_different_entry(
    client, device, cache_on, stub_youtube, stub_model, db
):
    _, headers, d = register(client, device=device)
    summarize(client, headers, d, lang="hi")
    summarize(client, headers, d, lang="en")
    langs = {row.lang for row in db.query(CachedOutput).all()}
    assert langs == {"hi", "en"}


def test_summary_and_notes_do_not_collide(client, device, cache_on, stub_youtube, stub_model, db):
    _, headers, d = register(client, device=device)
    summarize(client, headers, d)
    client.post(
        f"{API}/notes", json={"url": VIDEO, "device": d, "target_lang": "hi"}, headers=headers
    )
    modes = {row.mode for row in db.query(CachedOutput).all()}
    assert modes == {"summary", "notes"}


def test_bumping_the_prompt_version_retires_old_entries(
    client, device, cache_on, stub_youtube, stub_model, monkeypatch, db
):
    """Without this, improving a prompt would never reach a cached user."""
    _, headers, d = register(client, device=device)
    summarize(client, headers, d)
    assert stub_model["n"] == 1

    monkeypatch.setattr(output_cache, "PROMPT_VERSION", output_cache.PROMPT_VERSION + 1)
    summarize(client, headers, d)
    assert stub_model["n"] == 2, "old cached text was served after a prompt change"


def test_an_unlisted_video_is_never_cached(
    client, device, cache_on, stub_youtube, stub_model, monkeypatch, db
):
    """Someone's private video must not be handed to a stranger with the id."""
    async def not_public(video_id):
        return False

    monkeypatch.setattr("app.routers.summarize.youtube.is_public", not_public)

    _, headers, d = register(client, device=device)
    res = summarize(client, headers, d)
    assert res.status_code == 200
    assert db.query(CachedOutput).count() == 0, "an unlisted video was cached"


def test_a_failed_public_check_means_no_cache(
    client, device, cache_on, stub_youtube, stub_model, monkeypatch, db
):
    """If we cannot tell, the safe answer is not to share it."""
    async def blows_up(video_id):
        raise RuntimeError("YouTube blocked us")

    monkeypatch.setattr("app.routers.summarize.youtube.is_public", blows_up)

    _, headers, d = register(client, device=device)
    assert summarize(client, headers, d).status_code == 200
    assert db.query(CachedOutput).count() == 0


def test_caching_can_be_switched_off(client, device, stub_youtube, stub_model, monkeypatch, db):
    monkeypatch.setattr(settings, "OUTPUT_CACHE_ENABLED", False)
    _, headers, d = register(client, device=device)
    summarize(client, headers, d)
    summarize(client, headers, d)
    assert db.query(CachedOutput).count() == 0
    assert stub_model["n"] == 2


def test_a_too_short_answer_is_not_immortalised(db, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "OUTPUT_CACHE_MIN_CHARS", 200)
    stored = output_cache.put(db, VIDEO_ID, "summary", "hi", "m", "oops")
    assert stored is False
    assert db.query(CachedOutput).count() == 0


# ---------------------------------------------------------------------------
# Language detection without a transcript
# ---------------------------------------------------------------------------
def test_auto_language_is_answered_without_the_transcript(
    client, device, cache_on, stub_youtube, stub_model
):
    """"Same as the video" must also skip the transcript fetch on a hit -
    otherwise the slowest part of the request survives the cache."""
    from tests.conftest import make_device

    _, h1, d1 = register(client, email="one@example.com", device=device)
    summarize(client, h1, d1)                       # no target_lang => auto

    d2 = make_device(installation_id="auto-person")
    _, h2, _ = register(client, email="two@example.com", device=d2)
    before = stub_youtube["transcript"]
    res = summarize(client, h2, d2)                 # also auto

    assert events(res)[0]["cached"] is True
    assert stub_youtube["transcript"] == before


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
def test_purge_removes_only_cold_rows(db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "OUTPUT_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "OUTPUT_CACHE_MIN_CHARS", 1)

    output_cache.put(db, "coldvideo11", "summary", "hi", "m", "purana text hai")
    output_cache.put(db, "hotvideo111", "summary", "hi", "m", "naya text hai")

    cold = db.query(CachedOutput).filter_by(video_id="coldvideo11").one()
    cold.last_used_at = datetime.now(timezone.utc) - timedelta(days=400)
    db.commit()

    removed = output_cache.purge_stale(db, older_than_days=180)
    assert removed == 1
    left = {row.video_id for row in db.query(CachedOutput).all()}
    assert left == {"hotvideo111"}


def test_purge_endpoint_needs_the_admin_key(client):
    assert client.post(f"{API}/admin/cache/purge").status_code in (401, 403)


def test_stats_counts_rows_and_hits(db, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "OUTPUT_CACHE_MIN_CHARS", 1)
    output_cache.put(db, "statsvideo1", "summary", "hi", "m", "kuch text")
    output_cache.get(db, "statsvideo1", "summary", "hi", "m")
    output_cache.get(db, "statsvideo1", "summary", "hi", "m")

    s = output_cache.stats(db)
    assert s["rows"] == 1
    assert s["total_hits"] == 2
    assert s["prompt_version"] == output_cache.PROMPT_VERSION


# ---------------------------------------------------------------------------
# Stampede
# ---------------------------------------------------------------------------
def test_one_lock_object_per_key():
    async def check():
        a = await output_cache.lock_for(VIDEO_ID, "summary", "hi", "m")
        b = await output_cache.lock_for(VIDEO_ID, "summary", "hi", "m")
        c = await output_cache.lock_for(VIDEO_ID, "notes", "hi", "m")
        return a is b, a is c

    same, different = asyncio.run(check())
    assert same, "two callers for the same video got different locks"
    assert not different, "summary and notes shared a lock"


def test_idle_locks_are_dropped():
    """The lock dict must not grow forever on a busy server."""
    async def check():
        await output_cache.lock_for("gcvideo1234", "summary", "hi", "m")
        present = output_cache._key("gcvideo1234", "summary", "hi", "m") in output_cache._locks
        await output_cache.release_if_idle("gcvideo1234", "summary", "hi", "m")
        gone = output_cache._key("gcvideo1234", "summary", "hi", "m") not in output_cache._locks
        return present, gone

    present, gone = asyncio.run(check())
    assert present, "lock was never registered"
    assert gone, "an idle lock was left behind"


# ---------------------------------------------------------------------------
# The public/unlisted parser (no network)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "html,expected",
    [
        ('{"isFamilySafe":true,"isUnlisted":false}', True),
        ('{"isFamilySafe":true,"isUnlisted":true}', False),
        ('{"isFamilySafe":false,"isPrivate":true}', False),
        ('{"isFamilySafe":true,"status":"LOGIN_REQUIRED"}', False),
        ("<html>consent wall</html>", None),
        ("", None),
    ],
)
def test_looks_public(html, expected):
    assert youtube.looks_public(html) is expected
