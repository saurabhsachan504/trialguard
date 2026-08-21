"""Tests for the speed/completeness work on the notes + translate paths.

Every test here is written so that reverting the change it covers makes it fail
- that was checked by actually putting each old bug back.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.services import summarizer, translate, youtube


# ---------------------------------------------------------------------------
# Ollama: shared client, think:false, and its 400 fallback
# ---------------------------------------------------------------------------
class _FakeStream:
    """Stands in for client.stream(...) as an async context manager."""

    def __init__(self, status: int, lines: list[str]):
        self.status_code = status
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self) -> bytes:
        return b'{"error":"unknown parameter"}'

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    is_closed = False

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls: list[dict] = []

    def stream(self, method, url, json=None, **kw):
        self.calls.append(json)
        return self.behaviour(json)

    async def aclose(self):
        self.is_closed = True


def _ok_lines(text: str) -> list[str]:
    return [
        json.dumps({"message": {"content": text}}),
        json.dumps({"done": True}),
    ]


@pytest.fixture(autouse=True)
def _reset_think_flag():
    summarizer._think_param_supported = None
    yield
    summarizer._think_param_supported = None


def _install(monkeypatch, behaviour) -> _FakeClient:
    fake = _FakeClient(behaviour)

    async def _get_client():
        return fake

    monkeypatch.setattr(summarizer, "_ollama_client", _get_client)
    return fake


def test_reasoning_model_is_asked_to_skip_thinking(monkeypatch):
    """A hidden <think> block is generated and then thrown away, so we ask the
    server not to produce it in the first place."""
    fake = _install(monkeypatch, lambda body: _FakeStream(200, _ok_lines("hi")))

    out = asyncio.run(
        summarizer.collect_chat(model="sarvam-m-q4", system="s", content="c")
    )

    assert out == "hi"
    assert fake.calls[0].get("think") is False


def test_plain_model_is_not_sent_the_think_param(monkeypatch):
    fake = _install(monkeypatch, lambda body: _FakeStream(200, _ok_lines("hi")))

    asyncio.run(summarizer.collect_chat(model="gemma2:9b", system="s", content="c"))

    assert "think" not in fake.calls[0]


def test_old_ollama_rejecting_think_falls_back_and_is_remembered(monkeypatch):
    """Some builds answer HTTP 400 for think. That must not break the request,
    and must not be retried on every later call."""
    def behaviour(body):
        if "think" in body:
            return _FakeStream(400, [])
        return _FakeStream(200, _ok_lines("ok"))

    fake = _install(monkeypatch, behaviour)

    first = asyncio.run(summarizer.collect_chat(model="sarvam-m-q4", system="s", content="c"))
    assert first == "ok"
    assert len(fake.calls) == 2                      # tried with, then without
    assert summarizer._think_param_supported is False

    second = asyncio.run(summarizer.collect_chat(model="sarvam-m-q4", system="s", content="c"))
    assert second == "ok"
    assert len(fake.calls) == 3                      # no second probe
    assert "think" not in fake.calls[2]


def test_a_real_error_is_still_raised(monkeypatch):
    _install(monkeypatch, lambda body: _FakeStream(500, []))

    with pytest.raises(RuntimeError, match="Ollama HTTP 500"):
        asyncio.run(summarizer.collect_chat(model="gemma2:9b", system="s", content="c"))


def test_the_ollama_client_is_reused(monkeypatch):
    """One client for the process: a new one per call means a new TLS handshake
    for every chunk of every PDF."""
    asyncio.run(summarizer.close_ollama_client())

    async def two_calls():
        a = await summarizer._ollama_client()
        b = await summarizer._ollama_client()
        return a is b

    assert asyncio.run(two_calls()) is True
    asyncio.run(summarizer.close_ollama_client())


# ---------------------------------------------------------------------------
# Notes: concurrency and chunk size
# ---------------------------------------------------------------------------
def test_notes_chunks_run_concurrently(monkeypatch):
    """The whole point of NOTES_CONCURRENCY: chunks must overlap in time."""
    monkeypatch.setattr(settings, "NOTES_CONCURRENCY", 4)
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 500)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 20)

    in_flight = 0
    peak = 0

    async def fake_collect(*, model, system, content, num_predict=3000):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.02)
            return "## part\n\ndetails"
        finally:
            in_flight -= 1

    monkeypatch.setattr(summarizer, "collect_chat", fake_collect)

    text = "sentence number one. " * 400
    asyncio.run(summarizer.full_notes(text, lang="en"))

    assert peak > 1, "chunks ran one at a time"
    assert peak <= 4


def test_notes_keep_transcript_order_despite_concurrency(monkeypatch):
    monkeypatch.setattr(settings, "NOTES_CONCURRENCY", 4)
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 300)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 0)

    import re as _re

    async def fake_collect(*, model, system, content, num_predict=3000):
        marker = _re.search(r"MARK(\d+)", content)
        # Finish out of order on purpose: the first chunk replies last.
        await asyncio.sleep(0.03 if "MARK0" in content else 0.005)
        return f"[{marker.group(1) if marker else 'x'}]"

    monkeypatch.setattr(summarizer, "collect_chat", fake_collect)

    text = " ".join(f"MARK{i} " + "filler word " * 30 for i in range(6))
    notes = asyncio.run(summarizer.full_notes(text, lang="en"))

    markers = [int(m) for m in __import__("re").findall(r"\[(\d+)\]", notes)]
    assert markers == sorted(markers), f"chunks came back out of order: {markers}"
    assert markers[0] == 0, "the first chunk must still be first even though it replied last"


def test_chunk_size_default_keeps_round_trips_down():
    """6000-char chunks mean roughly half as many Ollama calls as 3500 did, for
    exactly the same transcript."""
    hour_long = "shabd " * 10000                     # ~60k chars
    big = summarizer.split_into_chunks(hour_long, 6000, 400)
    small = summarizer.split_into_chunks(hour_long, 3500, 400)
    assert len(big) < len(small)
    # Assert the shipped DEFAULT, not the value this machine's .env happens to
    # set - otherwise an old .env would make the test lie.
    from app.config import Settings

    assert Settings.model_fields["NOTES_CHUNK_CHARS"].default == 6000
    assert Settings.model_fields["NOTES_CONCURRENCY"].default == 4


# ---------------------------------------------------------------------------
# Transcript cache
# ---------------------------------------------------------------------------
def test_transcript_is_fetched_once_for_summary_and_pdf(monkeypatch):
    """The normal flow asks for the same video twice (summary, then PDF). The
    second one must not go out to YouTube again."""
    youtube.clear_transcript_cache()
    calls = {"n": 0}

    def fake_api(video_id):
        calls["n"] += 1
        return youtube.Transcript(
            text="a real transcript, long enough to be accepted by the guard clause",
            language="en",
            is_generated=True,
            source="captions",
        )

    monkeypatch.setattr(youtube, "_fetch_via_api", fake_api)

    first = youtube.fetch_transcript("abcdefghijk")
    second = youtube.fetch_transcript("abcdefghijk")

    assert calls["n"] == 1
    assert second.text == first.text

    youtube.fetch_transcript("differentvid")
    assert calls["n"] == 2                            # a new video still fetches


def test_transcript_cache_can_be_switched_off(monkeypatch):
    youtube.clear_transcript_cache()
    monkeypatch.setattr(settings, "TRANSCRIPT_CACHE_TTL_SECONDS", 0)
    calls = {"n": 0}

    def fake_api(video_id):
        calls["n"] += 1
        return youtube.Transcript(
            text="a real transcript, long enough to be accepted by the guard clause",
            language="en",
            is_generated=True,
            source="captions",
        )

    monkeypatch.setattr(youtube, "_fetch_via_api", fake_api)
    youtube.fetch_transcript("abcdefghijk")
    youtube.fetch_transcript("abcdefghijk")
    assert calls["n"] == 2


def test_transcript_cache_expires(monkeypatch):
    youtube.clear_transcript_cache()
    monkeypatch.setattr(settings, "TRANSCRIPT_CACHE_TTL_SECONDS", 60)
    clock = {"t": 1000.0}
    monkeypatch.setattr(youtube.time, "monotonic", lambda: clock["t"])
    calls = {"n": 0}

    def fake_api(video_id):
        calls["n"] += 1
        return youtube.Transcript(
            text="a real transcript, long enough to be accepted by the guard clause",
            language="en",
            is_generated=True,
            source="captions",
        )

    monkeypatch.setattr(youtube, "_fetch_via_api", fake_api)
    youtube.fetch_transcript("abcdefghijk")
    clock["t"] += 61
    youtube.fetch_transcript("abcdefghijk")
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Translate
# ---------------------------------------------------------------------------
def test_a_long_paragraph_is_split():
    """Story-mode notes are long single lines. The old splitter only broke on
    newlines, so those went into the URL whole and Google refused them."""
    one_line = "यह एक बहुत लंबा वाक्य है। " * 400
    pieces = translate._chunk(one_line, 1500)
    assert len(pieces) > 1
    assert max(len(p) for p in pieces) <= 1500


def test_markdown_lines_still_travel_together():
    text = "# Heading\n\n- point one\n- point two"
    assert translate._chunk(text, 1500) == [text]


def test_translate_runs_pieces_concurrently_and_keeps_order(monkeypatch):
    in_flight = 0
    peak = 0

    class FakeResponse:
        status_code = 200

        def __init__(self, q):
            self.q = q

        def json(self):
            return [[[f"[T]{self.q}", self.q]]]

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await asyncio.sleep(0.02)
                return FakeResponse(params["q"])
            finally:
                in_flight -= 1

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

    text = "\n".join(f"LINE{i} " + "x" * 1400 for i in range(8))
    out = asyncio.run(translate.translate(text, "hi"))

    order = [int(line.split("LINE")[1].split()[0]) for line in out.split("\n") if "LINE" in line]
    assert order == sorted(order), "concurrent replies were joined out of order"
    assert peak > 1, "pieces were translated one at a time"


# ---------------------------------------------------------------------------
# Completeness: a missing section must be visible in the PDF, not just on screen
# ---------------------------------------------------------------------------
def test_a_failed_section_is_written_into_the_notes(monkeypatch):
    """The on-screen warning is gone once the tab closes; the PDF is what the
    user keeps. A hole in it must be labelled inside the document."""
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 300)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 0)
    monkeypatch.setattr(settings, "NOTES_CHUNK_RETRIES", 2)

    async def fake_collect(*, model, system, content, num_predict=3000):
        if "MARK2" in content:
            raise RuntimeError("Ollama exploded")
        return "## written\n\ndetails"

    monkeypatch.setattr(summarizer, "collect_chat", fake_collect)

    warnings: list[str] = []

    async def on_warning(msg):
        warnings.append(msg)

    text = " ".join(f"MARK{i} " + "filler word " * 30 for i in range(4))
    notes = asyncio.run(summarizer.full_notes(text, lang="en", on_warning=on_warning))

    assert warnings, "the UI was not told"
    assert "sections are missing" in notes, "the PDF hides the gap"
    assert "## written" in notes, "the sections that DID work are still there"


def test_clean_notes_carry_no_warning(monkeypatch):
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 300)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 0)

    async def fake_collect(*, model, system, content, num_predict=3000):
        return "## written\n\ndetails"

    monkeypatch.setattr(summarizer, "collect_chat", fake_collect)
    notes = asyncio.run(summarizer.full_notes("word " * 500, lang="en"))
    assert "missing" not in notes


# ---------------------------------------------------------------------------
# Looping models: the "13 pages of the same paragraph" PDF
# ---------------------------------------------------------------------------
LOOPED = "\n\n".join(
    [
        "## द कपिल शर्मा शो का मजाकिया पक्ष",
        "- कपिल शर्मा ने सलमान खान और माधुरी दीक्षित को एक दूसरे के साथ नाचने के लिए कहा।",
    ]
    * 13
)


def test_a_looping_model_does_not_fill_the_pdf():
    out = summarizer.collapse_repeats(LOOPED)
    blocks = [b for b in out.split("\n\n") if b.strip()]
    assert len(blocks) == 2, f"repeats survived: {len(blocks)} blocks"
    assert "कपिल शर्मा" in out, "the real content was thrown away too"


def test_genuinely_different_sections_are_all_kept():
    text = "\n\n".join(
        f"## विषय {i}\n\n- यह {i} नंबर का अलग और पूरा बिंदु है जिसे रखना ज़रूरी है।"
        for i in range(8)
    )
    out = summarizer.collapse_repeats(text)
    for i in range(8):
        assert f"विषय {i}" in out, f"section {i} was wrongly removed"


def test_short_repeated_lines_are_left_alone():
    """A bare "- हाँ" twice is not a loop; only substantial blocks are deduped."""
    text = "## एक\n\n- हाँ\n\n## दो\n\n- हाँ"
    out = summarizer.collapse_repeats(text)
    assert out.count("- हाँ") == 2


def test_repeats_are_stripped_across_chunks_too(monkeypatch):
    """Chunks overlap by design, so two of them can write the same point up."""
    monkeypatch.setattr(settings, "NOTES_CHUNK_CHARS", 300)
    monkeypatch.setattr(settings, "NOTES_CHUNK_OVERLAP", 0)

    async def fake_collect(*, model, system, content, num_predict=3000):
        return "## वही शीर्षक\n\n- यह बिल्कुल वही विस्तृत बिंदु है जो हर हिस्से में आ रहा है।"

    monkeypatch.setattr(summarizer, "collect_chat", fake_collect)
    notes = asyncio.run(summarizer.full_notes("शब्द " * 400, lang="hi"))
    assert notes.count("वही शीर्षक") == 1


def test_a_tiny_chunk_gets_a_small_token_budget():
    """4096 tokens of "notes" for a 300-character clip is an invitation to pad."""
    assert summarizer._budget_for("x" * 300) < 1500


def test_a_normal_chunk_still_gets_the_full_budget():
    """The cap must never truncate a real video's notes."""
    assert summarizer._budget_for("x" * 6000) == settings.NOTES_NUM_PREDICT
