"""Web-app summarisation: URL parsing, language routing, trial accounting."""
from __future__ import annotations

import json

import pytest

from app.config import settings
from app.services import summarizer, youtube
from tests.conftest import register

API = settings.API_PREFIX

HINDI = "यह वीडियो भारत के इतिहास के बारे में है। " * 40
ENGLISH = "This video explains how photosynthesis works in plants. " * 40


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://vimeo.com/123456", None),
        ("https://www.youtube.com/watch?v=short", None),
        ("", None),
        ("not a url", None),
    ],
)
def test_extract_video_id(url, expected):
    assert youtube.extract_video_id(url) == expected


def test_clean_transcript_strips_caption_noise():
    raw = "[Music] hello   there [संगीत] friends [Applause]"
    assert youtube.clean_transcript(raw) == "hello there friends"


def test_sample_keeps_start_middle_and_end():
    text = "".join(f"{i:05d} " for i in range(4000))  # ~24k chars
    out = youtube.sample_for_model(text, 4000)
    assert len(out) < len(text)
    assert out.startswith("00000")
    assert "…" in out
    # a chunk from the last quarter survives
    assert "03" in out.rsplit("…", 1)[-1]


# ---------------------------------------------------------------------------
# Language routing
# ---------------------------------------------------------------------------
def test_language_detection_and_model_routing():
    assert summarizer.detect_language(HINDI) == "hi"
    assert summarizer.detect_language(ENGLISH) == "en"
    # The caption track's own code wins - it separates Hindi from Marathi,
    # which share the Devanagari script.
    assert summarizer.detect_language(HINDI, hint="mr") == "mr"
    assert summarizer.detect_language("", hint="ta-IN") == "ta"

    assert summarizer.model_for("en") == settings.OLLAMA_MODEL
    assert summarizer.model_for("hi") == settings.OLLAMA_MODEL
    assert summarizer.model_for("mr") == settings.OLLAMA_INDIC_MODEL
    assert summarizer.model_for("ta") == settings.OLLAMA_INDIC_MODEL


def test_language_directive_names_the_language():
    assert "Devanagari" in summarizer.language_directive("hi")
    assert "Tamil" in summarizer.language_directive("ta")
    assert "English" in summarizer.language_directive("en")


def test_strip_think_hides_reasoning_blocks():
    assert summarizer.strip_think("<think>plan</think>Answer") == "Answer"
    assert summarizer.strip_think("noise</think>Answer") == "Answer"
    # A block that is still open mid-stream must show nothing from it.
    assert summarizer.strip_think("Answer<think>still going") == "Answer"


def test_chunking_covers_everything_with_overlap():
    text = "word " * 4000  # 20k chars
    chunks = summarizer.split_into_chunks(text, 6000, 250)
    assert len(chunks) >= 3
    assert sum(len(c) for c in chunks) >= len(text.strip())


# ---------------------------------------------------------------------------
# Endpoints (transcript + model are stubbed - no network in tests)
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_youtube(monkeypatch):
    async def fake_meta(video_id):
        return youtube.VideoMeta(
            video_id=video_id,
            title="Test video",
            author="Test channel",
            thumbnail=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            url=youtube.watch_url(video_id),
        )

    def fake_transcript(video_id):
        return youtube.Transcript(
            text=HINDI, language="hi", is_generated=True, source="captions"
        )

    monkeypatch.setattr("app.routers.summarize.youtube.fetch_metadata", fake_meta)
    monkeypatch.setattr("app.routers.summarize.youtube.fetch_transcript", fake_transcript)


@pytest.fixture
def stub_model(monkeypatch):
    async def fake_stream(**kwargs):
        for piece in ["## अवलोकन\n", "यह एक ", "परीक्षण सारांश है।"]:
            yield piece

    monkeypatch.setattr(summarizer, "stream_chat", fake_stream)


def read_events(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_summarize_streams_and_charges_one_trial(client, device, stub_youtube, stub_model):
    _, headers, _ = register(client, device=device)

    res = client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device, "mode": "summary"},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    events = read_events(res)

    meta = events[0]
    assert meta["type"] == "meta"
    assert meta["language"] == "hi"
    assert meta["language_name"] == "Hindi"
    assert meta["video"]["title"] == "Test video"
    assert meta["entitlement"]["trials_remaining"] == 4

    done = events[-1]
    assert done["type"] == "done"
    assert "परीक्षण सारांश" in done["text"]
    assert "".join(e["text"] for e in events if e["type"] == "delta") == done["text"]


def test_same_video_twice_is_charged_once(client, device, stub_youtube, stub_model):
    _, headers, _ = register(client, device=device)
    body = {"url": "https://youtu.be/dQw4w9WgXcQ", "device": device, "mode": "summary"}

    client.post(f"{API}/summarize", json=body, headers=headers)
    # Different mode, same video -> still one charge.
    res = client.post(
        f"{API}/summarize", json={**body, "mode": "key_points"}, headers=headers
    )
    meta = read_events(res)[0]
    assert meta["entitlement"]["trials_used"] == 1
    assert meta["entitlement"]["trials_remaining"] == 4


def test_pdf_notes_do_not_charge_again_for_the_same_video(
    client, device, stub_youtube, stub_model
):
    _, headers, _ = register(client, device=device)
    client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device, "mode": "summary"},
        headers=headers,
    )
    res = client.post(
        f"{API}/notes",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device},
        headers=headers,
    )
    assert res.status_code == 200
    # A streaming endpoint returns 200 even when the body carries an error, so
    # the status alone proves nothing - check that real notes actually came out.
    events = read_events(res)
    assert not [e for e in events if e["type"] == "error"], events
    assert events[-1]["type"] == "done"
    assert events[-1]["text"].strip()

    ent = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert ent["trials_used"] == 1


def test_five_videos_then_402(client, device, stub_youtube, stub_model):
    _, headers, _ = register(client, device=device)
    ids = ["dQw4w9WgXcQ", "jNQXAC9IVRw", "9bZkp7q19f0", "kJQP7kiw5Fk", "fJ9rUzIMcZQ"]
    for vid in ids:
        res = client.post(
            f"{API}/summarize",
            json={"url": f"https://youtu.be/{vid}", "device": device},
            headers=headers,
        )
        assert res.status_code == 200

    res = client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/L_jWHffIx5E", "device": device},
        headers=headers,
    )
    assert res.status_code == 402
    assert "$5/month" in res.json()["detail"]["message"]


def test_summarize_requires_auth(client, device):
    res = client.post(
        f"{API}/summarize", json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device}
    )
    assert res.status_code == 401


def test_bad_url_is_rejected_before_charging(client, device):
    _, headers, _ = register(client, device=device)
    res = client.post(
        f"{API}/summarize",
        json={"url": "https://vimeo.com/123", "device": device},
        headers=headers,
    )
    assert res.status_code == 400
    ent = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert ent["trials_used"] == 0


def test_missing_transcript_is_reported_clearly(client, device, monkeypatch, stub_youtube):
    _, headers, _ = register(client, device=device)

    def boom(video_id):
        raise youtube.TranscriptUnavailable("no captions on this video")

    monkeypatch.setattr("app.routers.summarize.youtube.fetch_transcript", boom)
    res = client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device},
        headers=headers,
    )
    assert res.status_code == 422
    assert "no captions" in res.json()["detail"]


def test_model_failure_becomes_a_friendly_error_event(
    client, device, stub_youtube, monkeypatch
):
    _, headers, _ = register(client, device=device)

    async def broken(**kwargs):
        raise RuntimeError("Ollama HTTP 404: model not found")
        yield  # pragma: no cover

    monkeypatch.setattr(summarizer, "stream_chat", broken)
    res = client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device},
        headers=headers,
    )
    events = read_events(res)
    assert events[-1]["type"] == "error"
    assert "not installed" in events[-1]["message"]


def test_video_info_is_free(client, device, stub_youtube):
    _, headers, _ = register(client, device=device)
    res = client.post(
        f"{API}/video/info",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device},
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["title"] == "Test video"

    ent = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert ent["trials_used"] == 0


def test_home_page_is_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "YouTube Summarizer" in res.text
    assert res.headers["content-type"].startswith("text/html")


# ---------------------------------------------------------------------------
# Language: the bug where a Hindi video came back in English
# ---------------------------------------------------------------------------
from app.services import translate as tr  # noqa: E402


def test_prompt_labels_are_localised_not_english():
    """The English template was the bug: the model copied '## Overview' and then
    kept writing English. Labels must arrive already translated."""
    hi = summarizer.summary_prompt("hi")
    assert "अवलोकन" in hi and "मुख्य बिंदु" in hi
    assert "## Overview" not in hi
    assert "**Key Point:**" not in hi

    ta = summarizer.summary_prompt("ta")
    assert "மேலோட்டம்" in ta and "## Overview" not in ta

    # A language we have no label table for still gets an explicit instruction.
    sw = summarizer.summary_prompt("sw")
    assert "Swahili" in sw


def test_language_rule_is_first_and_last_in_the_system_prompt(monkeypatch):
    captured = {}

    async def fake_stream(*, model, system, content, num_predict=3000):
        captured["system"] = system
        captured["model"] = model
        yield "ok"

    monkeypatch.setattr(summarizer, "stream_chat", fake_stream)

    import asyncio

    async def run():
        async for _ in summarizer.stream_summary(HINDI, lang="hi", mode="summary"):
            pass

    asyncio.run(run())
    assert captured["system"].startswith("ABSOLUTE LANGUAGE RULE")
    assert "FINAL REMINDER" in captured["system"]
    assert "Hindi" in captured["system"]


@pytest.mark.parametrize(
    "target,model,write_lang,translate_to",
    [
        ("hi", settings.OLLAMA_MODEL, "hi", None),
        ("en", settings.OLLAMA_MODEL, "en", None),
        ("ta", settings.OLLAMA_INDIC_MODEL, "ta", None),
        ("mr", settings.OLLAMA_INDIC_MODEL, "mr", None),
        # A language neither model writes well: produce English, then translate.
        ("sw", settings.OLLAMA_MODEL, "en", "sw"),
        ("si", settings.OLLAMA_MODEL, "en", "si"),
    ],
)
def test_plan_for_language(target, model, write_lang, translate_to):
    assert summarizer.plan_for(target) == (model, write_lang, translate_to)


def test_script_check_catches_wrong_language_output():
    english = "This video explains the history of the Indian freedom struggle in detail."
    hindi = "यह वीडियो भारत के स्वतंत्रता संग्राम के इतिहास को विस्तार से समझाता है।"

    assert tr.script_matches(hindi, "hi")
    assert not tr.script_matches(english, "hi")      # the reported bug
    assert tr.script_matches(english, "en")
    assert tr.script_matches("இந்த வீடியோ விரிவாக விளக்குகிறது என்பதை நன்கு காட்டுகிறது", "ta")
    assert not tr.script_matches(english, "ta")
    # Too short to judge -> never rejected.
    assert tr.script_matches("ok", "hi")


def test_wrong_language_output_is_auto_translated(client, device, stub_youtube, monkeypatch):
    """End to end: model answers in English for a Hindi video -> we fix it."""
    _, headers, _ = register(client, device=device)

    async def english_stream(**kwargs):
        yield "## Overview\nThis video explains the concept of Manonash in Indian philosophy."

    async def fake_translate(text, target):
        assert target == "hi"
        return "## अवलोकन\nयह वीडियो भारतीय दर्शन में मनोनाश की अवधारणा समझाता है।"

    monkeypatch.setattr(summarizer, "stream_chat", english_stream)
    monkeypatch.setattr(tr, "translate", fake_translate)

    res = client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device},
        headers=headers,
    )
    events = read_events(res)
    assert any(e["type"] == "status" and "wrong language" in e["message"] for e in events)
    assert "अवलोकन" in events[-1]["text"]
    assert events[-1]["language"] == "hi"


def test_target_lang_overrides_the_video_language(client, device, stub_youtube, stub_model):
    _, headers, _ = register(client, device=device)
    res = client.post(
        f"{API}/summarize",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device, "target_lang": "ta"},
        headers=headers,
    )
    meta = read_events(res)[0]
    assert meta["detected_language"] == "hi"
    assert meta["language"] == "ta"
    assert meta["model"] == settings.OLLAMA_INDIC_MODEL


def test_translate_endpoint_does_not_charge_a_trial(client, device, monkeypatch):
    _, headers, _ = register(client, device=device)

    async def fake_translate(text, target):
        return "अनुवादित पाठ"

    monkeypatch.setattr(tr, "translate", fake_translate)
    res = client.post(
        f"{API}/translate",
        json={"text": "Some English summary text here.", "target_lang": "hi"},
        headers=headers,
    )
    assert res.status_code == 200
    assert res.json()["text"] == "अनुवादित पाठ"
    assert res.json()["language_name"] == "Hindi"

    ent = client.post(
        f"{API}/entitlement/check", json={"device": device}, headers=headers
    ).json()
    assert ent["trials_used"] == 0


def test_translate_chunking_preserves_line_structure():
    md = "\n".join(f"- point number {i} with some words" for i in range(200))
    chunks = tr._chunk(md, size=500)
    assert len(chunks) > 1
    assert "\n".join(chunks) == md          # nothing lost, nothing reordered
    assert all(len(c) <= 560 for c in chunks)



def test_full_notes_actually_produce_text(client, device, stub_youtube, monkeypatch):
    """Guards the bug where the notes prompts were missing and every chunk died."""
    calls = []

    async def fake_stream(*, model, system, content, num_predict=3000):
        calls.append(system)
        yield "# विषय\n\n## भाग एक\nविस्तृत नोट्स यहाँ हैं।"

    monkeypatch.setattr(summarizer, "stream_chat", fake_stream)
    _, headers, _ = register(client, device=device)

    res = client.post(
        f"{API}/notes",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device},
        headers=headers,
    )
    events = read_events(res)
    assert not [e for e in events if e["type"] == "error"], events
    assert "विस्तृत नोट्स" in events[-1]["text"]
    # The language rule must wrap the notes prompt, exactly like the summary.
    assert calls and calls[0].startswith("ABSOLUTE LANGUAGE RULE")
    assert "exhaustive" in calls[0]


def test_notes_prompts_exist_and_carry_no_stray_english_labels():
    for prompt in (summarizer.NOTES_FIRST_PROMPT, summarizer.NOTES_SEGMENT_PROMPT):
        assert "## Heading" in prompt          # shape, not literal text
        assert "## Overview" not in prompt     # no copyable English heading


def test_notes_respects_target_lang(client, device, stub_youtube, monkeypatch):
    async def fake_stream(**kwargs):
        yield "# தலைப்பு\n\n## பகுதி ஒன்று\nவிரிவான குறிப்புகள்."

    monkeypatch.setattr(summarizer, "stream_chat", fake_stream)
    _, headers, _ = register(client, device=device)

    res = client.post(
        f"{API}/notes",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "device": device, "target_lang": "ta"},
        headers=headers,
    )
    events = read_events(res)
    assert events[0]["language"] == "ta"
    assert events[0]["detected_language"] == "hi"
    assert events[-1]["type"] == "done"



# ---------------------------------------------------------------------------
# Full notes must cover the WHOLE video - nothing silently dropped
# ---------------------------------------------------------------------------
def test_notes_read_the_entire_transcript_not_a_sample(monkeypatch):
    """A 3-hour transcript must be fully covered, in order, with overlap."""
    import asyncio

    seen: list[str] = []

    async def fake_chat(*, model, system, content, num_predict=3000):
        seen.append(content)
        return f"## part {len(seen)}\nnotes"

    monkeypatch.setattr(summarizer, "collect_chat", fake_chat)

    # ~200k chars - well past the old 30-chunk / 180k ceiling.
    transcript = " ".join(f"sentence{i}." for i in range(20000))
    out = asyncio.run(summarizer.full_notes(transcript, lang="en"))

    joined = "".join(seen)
    # Every sentence of the source reached the model.
    for probe in ("sentence0.", "sentence9999.", "sentence19999."):
        assert probe in joined, probe
    # Chunks are in order and overlap, so nothing falls between two of them.
    assert len(seen) > 30
    assert len(joined) > len(transcript)
    assert out.count("## part") == len(seen)


def test_notes_chunk_cap_is_off_by_default_and_warns_when_set(monkeypatch):
    import asyncio

    async def fake_chat(**kwargs):
        return "notes"

    monkeypatch.setattr(summarizer, "collect_chat", fake_chat)
    assert settings.NOTES_MAX_CHUNKS == 0, "a silent cap would lose content"

    warnings: list[str] = []

    async def on_warning(msg):
        warnings.append(msg)

    monkeypatch.setattr(settings, "NOTES_MAX_CHUNKS", 3)
    transcript = "word " * 20000
    asyncio.run(summarizer.full_notes(transcript, lang="en", on_warning=on_warning))
    assert warnings and "left out" in warnings[0]
    assert "NOTES_MAX_CHUNKS" in warnings[0]


def test_a_failing_chunk_is_reported_not_silently_dropped(monkeypatch):
    import asyncio

    calls = {"n": 0}

    async def flaky(*, model, system, content, num_predict=3000):
        calls["n"] += 1
        if "BOOM" in content:
            raise RuntimeError("model exploded")
        return "## ok\nnotes"

    monkeypatch.setattr(summarizer, "collect_chat", flaky)

    warnings: list[str] = []

    async def on_warning(msg):
        warnings.append(msg)

    transcript = ("good " * 700) + ("BOOM " * 700) + ("good " * 700)
    out = asyncio.run(
        summarizer.full_notes(transcript, lang="en", on_warning=on_warning)
    )
    assert out                       # the healthy parts still come through
    assert warnings, "a lost section must be reported"
    assert "could not be written" in warnings[0]


def test_notes_keep_chunk_order_even_when_run_concurrently(monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "NOTES_CONCURRENCY", 4)

    async def slow_for_early_chunks(*, model, system, content, num_predict=3000):
        # Make the first chunk the slowest: if ordering were by completion
        # time, the notes would come out shuffled.
        marker = content.strip().split()[0]
        await asyncio.sleep(0.05 if marker == "aaa" else 0.0)
        return f"## {marker}"

    monkeypatch.setattr(summarizer, "collect_chat", slow_for_early_chunks)

    transcript = ("aaa " * 900) + ("bbb " * 900) + ("ccc " * 900)
    out = asyncio.run(summarizer.full_notes(transcript, lang="en"))
    headings = [line for line in out.splitlines() if line.startswith("## ")]
    assert headings == sorted(headings), headings


def test_notes_progress_counts_every_chunk(monkeypatch):
    import asyncio

    async def fake_chat(**kwargs):
        return "notes"

    monkeypatch.setattr(summarizer, "collect_chat", fake_chat)
    seen: list[tuple[int, int]] = []

    async def on_progress(done, total):
        seen.append((done, total))

    transcript = "word " * 6000
    asyncio.run(
        summarizer.full_notes(transcript, lang="en", on_progress=on_progress)
    )
    total = seen[0][1]
    assert sorted(d for d, _ in seen) == list(range(1, total + 1))
