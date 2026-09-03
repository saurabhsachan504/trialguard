"""Summary generation via Ollama, in the video's own language.

The prompts and the language routing are carried over from the extension, which
already tuned them: the summary must come back in the SAME language the video is
spoken in, and regional Indian languages go to an Indic-strong model because
general models drift back into English or produce broken text.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator

import httpx

from app.config import settings
from app.services.youtube import sample_for_model

logger = logging.getLogger("trialguard.summarizer")

# Languages the general model writes well natively.
STRONG_LANGS = {
    "en", "hi", "es", "fr", "de", "it", "pt", "ru", "ja", "zh", "ar", "nl", "tr", "ko", "id", "vi",
}
# Regional Indian languages -> the Indic model.
INDIC_LANGS = {
    "mr", "gu", "pa", "ta", "te", "kn", "ml", "bn", "or", "as", "ur", "sd", "ne", "kok", "mai",
}

LANG_NAMES = {
    "en": "English", "hi": "Hindi", "fr": "French", "es": "Spanish", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian", "ar": "Arabic", "ur": "Urdu",
    "fa": "Persian", "tr": "Turkish", "nl": "Dutch", "pl": "Polish", "uk": "Ukrainian",
    "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "ms": "Malay", "he": "Hebrew",
    "el": "Greek", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "bn": "Bengali",
    "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam", "mr": "Marathi", "pa": "Punjabi",
    "ta": "Tamil", "te": "Telugu", "or": "Odia", "as": "Assamese", "ne": "Nepali",
    "si": "Sinhala", "sw": "Swahili", "ro": "Romanian", "cs": "Czech", "sv": "Swedish",
    "hu": "Hungarian", "fi": "Finnish", "da": "Danish", "no": "Norwegian",
}

# Scripts that map to exactly one language - an instant, offline detection.
_SCRIPT_RANGES = [
    (re.compile(r"[઀-૿]"), "gu"),
    (re.compile(r"[਀-੿]"), "pa"),
    (re.compile(r"[஀-௿]"), "ta"),
    (re.compile(r"[ఀ-౿]"), "te"),
    (re.compile(r"[ಀ-೿]"), "kn"),
    (re.compile(r"[ഀ-ൿ]"), "ml"),
    (re.compile(r"[ঀ-৿]"), "bn"),
    (re.compile(r"[଀-୿]"), "or"),
    (re.compile(r"[؀-ۿ]"), "ar"),
]
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def detect_language(text: str, hint: str | None = None) -> str:
    """Language code for the summary.

    The caption track's own code is the most reliable signal (it distinguishes
    Hindi from Marathi, which share a script), so it wins when present.
    """
    if hint:
        code = hint.split("-")[0].lower()
        if code:
            return code

    sample = (text or "")[:4000]
    for pattern, code in _SCRIPT_RANGES:
        if len(pattern.findall(sample)) >= 15:
            return code
    if len(_DEVANAGARI.findall(sample)) >= 15:
        return "hi"
    return "en"


def language_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


def model_for(code: str) -> str:
    if code in INDIC_LANGS:
        return settings.OLLAMA_INDIC_MODEL
    return settings.OLLAMA_MODEL


def plan_for(target: str) -> tuple[str, str, str | None]:
    """Decide (model, generation_language, translate_to) for a target language.

    Three cases, same as the extension:
      * Regional Indian language -> the Indic model writes it natively.
      * A language the general model handles well -> write it directly.
      * Anything else -> write in English, then translate. Forcing a weak model
        into a language it writes badly produces broken text; translating clean
        English is far better.
    """
    if target in INDIC_LANGS:
        return settings.OLLAMA_INDIC_MODEL, target, None
    if target in STRONG_LANGS:
        return settings.OLLAMA_MODEL, target, None
    return settings.OLLAMA_MODEL, "en", target


def language_directive(code: str) -> str:
    name = language_name(code)
    if code == "hi":
        return (
            "ABSOLUTE LANGUAGE RULE: Write the ENTIRE output in natural, correct Hindi "
            "using Devanagari (शुद्ध, सरल हिंदी). Every heading, label and sentence must "
            "be in Hindi. Do NOT write English sentences (well-known proper nouns and "
            "technical terms may keep their usual form). Correct spelling, grammar, "
            "मात्राएँ and genders."
        )
    if code == "en":
        return (
            "ABSOLUTE LANGUAGE RULE: Write the ENTIRE output in clear English. Do NOT "
            "use any other script anywhere - not even a single word or heading."
        )
    return (
        f"ABSOLUTE LANGUAGE RULE: The video is in {name}. Write the ENTIRE output in "
        f"{name} ONLY. Every heading, label and sentence MUST be in {name}. Do NOT write "
        f"in English or any other language. Use correct, natural {name} grammar and "
        "spelling. Well-known proper nouns and technical terms may keep their usual form."
    )


# ---------------------------------------------------------------------------
# Prompts
#
# The section labels are injected ALREADY TRANSLATED. That matters: an earlier
# version left literal English labels ("## Overview", "**Key Point:**") in the
# template and just asked the model to translate them. Models copy the template
# verbatim, and once the first heading is English the whole answer continues in
# English - which is exactly the bug this fixes.
# ---------------------------------------------------------------------------
LABELS = {
    "en": {"overview": "Overview", "key_point": "Key Point", "background": "Background",
           "details": "Details", "conclusion": "Conclusion & Key Takeaways",
           "in_summary": "In summary", "key_points": "Key Points"},
    "hi": {"overview": "अवलोकन", "key_point": "मुख्य बिंदु", "background": "पृष्ठभूमि",
           "details": "विशेष विवरण", "conclusion": "निष्कर्ष और मुख्य बातें",
           "in_summary": "संक्षेप में", "key_points": "मुख्य बिंदु"},
    "mr": {"overview": "आढावा", "key_point": "मुख्य मुद्दा", "background": "पार्श्वभूमी",
           "details": "तपशील", "conclusion": "निष्कर्ष आणि महत्त्वाचे मुद्दे",
           "in_summary": "थोडक्यात", "key_points": "मुख्य मुद्दे"},
    "gu": {"overview": "ઝાંખી", "key_point": "મુખ્ય મુદ્દો", "background": "પૃષ્ઠભૂમિ",
           "details": "વિગતો", "conclusion": "નિષ્કર્ષ અને મુખ્ય બાબતો",
           "in_summary": "ટૂંકમાં", "key_points": "મુખ્ય મુદ્દા"},
    "bn": {"overview": "সংক্ষিপ্ত বিবরণ", "key_point": "মূল বিষয়", "background": "পটভূমি",
           "details": "বিস্তারিত", "conclusion": "উপসংহার ও মূল বিষয়",
           "in_summary": "সংক্ষেপে", "key_points": "মূল বিষয়সমূহ"},
    "ta": {"overview": "மேலோட்டம்", "key_point": "முக்கிய கருத்து", "background": "பின்னணி",
           "details": "விவரங்கள்", "conclusion": "முடிவும் முக்கிய கருத்துகளும்",
           "in_summary": "சுருக்கமாக", "key_points": "முக்கிய கருத்துகள்"},
    "te": {"overview": "అవలోకనం", "key_point": "ముఖ్య అంశం", "background": "నేపథ్యం",
           "details": "వివరాలు", "conclusion": "ముగింపు మరియు ముఖ్యాంశాలు",
           "in_summary": "సంక్షిప్తంగా", "key_points": "ముఖ్య అంశాలు"},
    "kn": {"overview": "ಅವಲೋಕನ", "key_point": "ಮುಖ್ಯ ಅಂಶ", "background": "ಹಿನ್ನೆಲೆ",
           "details": "ವಿವರಗಳು", "conclusion": "ತೀರ್ಮಾನ ಮತ್ತು ಮುಖ್ಯಾಂಶಗಳು",
           "in_summary": "ಸಂಕ್ಷಿಪ್ತವಾಗಿ", "key_points": "ಮುಖ್ಯ ಅಂಶಗಳು"},
    "ml": {"overview": "അവലോകനം", "key_point": "പ്രധാന ആശയം", "background": "പശ്ചാത്തലം",
           "details": "വിശദാംശങ്ങൾ", "conclusion": "നിഗമനവും പ്രധാന കാര്യങ്ങളും",
           "in_summary": "ചുരുക്കത്തിൽ", "key_points": "പ്രധാന ആശയങ്ങൾ"},
    "pa": {"overview": "ਸੰਖੇਪ", "key_point": "ਮੁੱਖ ਨੁਕਤਾ", "background": "ਪਿਛੋਕੜ",
           "details": "ਵੇਰਵੇ", "conclusion": "ਸਿੱਟਾ ਅਤੇ ਮੁੱਖ ਗੱਲਾਂ",
           "in_summary": "ਸੰਖੇਪ ਵਿੱਚ", "key_points": "ਮੁੱਖ ਨੁਕਤੇ"},
    "ur": {"overview": "جائزہ", "key_point": "اہم نکتہ", "background": "پس منظر",
           "details": "تفصیلات", "conclusion": "نتیجہ اور اہم باتیں",
           "in_summary": "خلاصہ یہ کہ", "key_points": "اہم نکات"},
}


def labels_for(code: str) -> dict[str, str]:
    """Localised labels, or English ones plus an explicit translate instruction."""
    return LABELS.get(code, LABELS["en"])


def _label_rule(code: str) -> str:
    if code in LABELS or code == "en":
        return ""
    name = language_name(code)
    return (
        f"\n- The section labels below are shown in English only as a guide. Write "
        f"every label in {name} instead - do not leave any English label in the output."
    )


def summary_prompt(code: str) -> str:
    L = labels_for(code)
    return f"""Write a CONCISE but complete summary of the video from the content below, in Markdown. TARGET LENGTH: about 350-400 words TOTAL (never more than ~420 words). Be selective - capture the MAIN parts of the whole video from start to end; do not list every tiny detail.

Use EXACTLY this structure, with these exact labels:

## {L["overview"]}
A 3-5 sentence paragraph on what the whole video is about.

Then 4 to 7 sections (NOT more). For each, YOU choose a real, short, descriptive title in the SAME language as the rest of the output. NEVER output the words "Section Title" or any angle-bracket placeholder - always write a real title:

## <your real descriptive title>
**{L["key_point"]}:** one sentence.

- **{L["background"]}:** 1-2 sentences of context.
- **{L["details"]}:** 1-2 sentences with the real names, numbers and facts.

Then finish with:

## {L["conclusion"]}
**{L["key_point"]}:** one sentence.

- **{L["details"]}:** 1-2 sentences on the key takeaways.

**{L["in_summary"]},** a 2-3 sentence closing paragraph.

Strict rules:
- Keep the WHOLE summary around 350-400 words. Choose only the 4-7 most important sections; merge related topics; do NOT repeat the same point in multiple sections.
- Be FAITHFUL and ACCURATE: real names, events, dates and numbers exactly; never confuse two people or events, never invent.
- NEVER include caption noise like "[Music]" or anything in square brackets.
- Start directly with "## {L["overview"]}". Always use REAL section titles, never a placeholder.{_label_rule(code)}"""


def key_points_prompt(code: str) -> str:
    L = labels_for(code)
    return f"""From the video content below, write the key points ONLY, in Markdown.

- Start with "## {L["key_points"]}".
- 6 to 10 numbered points, in the order they are discussed.
- Each point: bold the core idea in 3-6 words, then explain it in one or two sentences with the real names, numbers and facts.
- No introduction, no conclusion, no filler like "the video discusses".
- Never include anything in square brackets.{_label_rule(code)}"""

def prompt_for(mode: str, code: str) -> str:
    return key_points_prompt(code) if mode == "key_points" else summary_prompt(code)



# ---------------------------------------------------------------------------
# Full-notes prompts (the PDF). These describe the SHAPE of the output rather
# than giving literal labels, so there is no English text for the model to copy
# - the language directive wrapped around them decides the language.
# ---------------------------------------------------------------------------
_NOTES_FORMAT_RULE = """- COMPLETENESS IS THE WHOLE POINT. These notes replace watching the video. A reader who only has your notes must learn EVERYTHING the video taught, in the same order.
- Do NOT summarise, shorten, generalise or "cover the highlights". Write up every single thing in this part: every claim, example, story, name, place, date, number, definition, step, comparison, warning, aside, question asked, answer given, quotation and conclusion.
- There is NO length limit. If this part of the transcript contains 15 distinct points, write all 15. Long output is correct output - never stop early to keep it short.
- If something is repeated in the transcript for emphasis, write it once, clearly - but never drop a point because it "sounds similar" to another one.
- Keep the speaker's own examples and phrasing where they carry meaning; do not replace a concrete example with a vague description of it.
- CHOOSE THE SHAPE FROM THE CONTENT TYPE:
  \u2022 STORY / narrative / biography told as a story: each "## Heading" followed by flowing detailed PARAGRAPHS (3-6 sentences each). No bullet points for story content.
  \u2022 INFORMATIONAL (lesson, tutorial, documentary, travel, news, how-to, a facts podcast, comparisons, lists): under each "## Heading", a short intro line where it helps, PLUS bullet points where each bullet bolds the **key idea / term** and then explains it in 1-2 clear sentences.
  \u2022 Use "### Sub-headings" freely to group related points - more structure is better than less.
- Every heading and sub-heading must be in the output language - never leave one in English.
- You MAY bold **key names, terms, dates, numbers**.
- STYLE FOR ALL AGES: write so a school student, a child and an elderly reader all find it easy and interesting."""

NOTES_FIRST_PROMPT = f"""You are writing COMPLETE, exhaustive study notes for a video - nothing may be missed. This is the FIRST part of the transcript.

- Begin with "# <a concise, clear topic title>" then a 2-3 sentence introduction PARAGRAPH about the whole video.
- Then cover THIS part fully in "## Heading" sections, per the rules below.
{_NOTES_FORMAT_RULE}
- Do not invent anything that is not in the transcript.
- Do NOT write a final conclusion - more parts follow.
- Never include caption noise like "[Music]" or anything in square brackets."""

NOTES_SEGMENT_PROMPT = f"""Continue the COMPLETE, exhaustive notes for the SAME video. This is a LATER part of its transcript.

- Write up THIS part in full, in "## Heading" sections, continuing naturally from the earlier parts.
- The first sentence or two may overlap with the previous part - do not repeat what was already written, start from where the new material begins.
- Do NOT repeat the title or an overall introduction. Do NOT add a final wrap-up unless this is clearly the very end of the video.
{_NOTES_FORMAT_RULE}
- Do not invent anything that is not in the transcript.
- Never include caption noise like "[Music]" or anything in square brackets."""

# ---------------------------------------------------------------------------
# Reasoning models emit a hidden <think> block first - never show it.
# ---------------------------------------------------------------------------
def is_reasoning_model(name: str) -> bool:
    return bool(re.search(r"sarvam|deepseek-r1|qwq|reason|think", str(name or ""), re.I))


def strip_think(text: str) -> str:
    if not text:
        return text
    out = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.I)
    idx = out.rfind("</think>")
    if idx >= 0:
        out = out[idx + len("</think>") :]
    open_idx = out.lower().find("<think>")
    if open_idx >= 0:
        out = out[:open_idx]
    return out.strip()


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------
async def stream_chat(
    *, model: str, system: str, content: str, num_predict: int = 3000
) -> AsyncIterator[str]:
    """Yield tokens from Ollama's /api/chat as they arrive."""
    url = settings.OLLAMA_URL.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "stream": True,
        "keep_alive": -1,
        "options": {
            "temperature": 0.4,
            "top_p": 0.9,
            "repeat_penalty": 1.15,
            "num_predict": num_predict,
            # The 4096 default is too small: the transcript fills it and the
            # summary gets cut off mid-sentence.
            "num_ctx": 8192,
        },
    }

    # Sochne wale models ko soch band karne ko kaho. Warna wo pehle 20 second
    # tak `thinking` bhejta hai jisme `content` khaali hota hai, aur user ko
    # khaali screen dikhti hai. Key sirf band karne ke liye bheji jaati hai -
    # jo models sochte hi nahi unhe isse koi farq nahi padta.
    if getattr(settings, "OLLAMA_SKIP_THINKING", True):
        payload["think"] = False

    timeout = httpx.Timeout(settings.OLLAMA_TIMEOUT_SECONDS, connect=15)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload) as res:
            if res.status_code != 200:
                body = (await res.aread()).decode("utf-8", "replace")[:300]
                raise RuntimeError(f"Ollama HTTP {res.status_code}: {body}")
            async for line in res.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = (obj.get("message") or {}).get("content")
                if token:
                    yield token
                if obj.get("done"):
                    return


async def collect_chat(*, model: str, system: str, content: str, num_predict: int = 3000) -> str:
    parts: list[str] = []
    async for token in stream_chat(
        model=model, system=system, content=content, num_predict=num_predict
    ):
        parts.append(token)
    return strip_think("".join(parts))


async def stream_summary(
    transcript: str, *, lang: str, mode: str = "summary"
) -> AsyncIterator[str]:
    """Stream the on-screen summary, hiding any reasoning block as it goes."""
    model = model_for(lang)
    # Language rule first (highest priority), prompt second, reminder last -
    # models weight the opening and closing of a system prompt most heavily.
    system = language_directive(lang) + "\n\n" + prompt_for(mode, lang)
    if lang != "en":
        system += (
            f"\n\nFINAL REMINDER: every single word of your answer - headings, labels, "
            f"body text - must be in {language_name(lang)}. Do not write in English."
        )

    hide_reasoning = is_reasoning_model(model)
    buffer = ""
    emitted = 0

    async for token in stream_chat(
        model=model,
        system=system,
        content=sample_for_model(transcript, settings.TRANSCRIPT_MAX_CHARS),
        num_predict=3000,
    ):
        buffer += token
        if hide_reasoning and "</think>" not in buffer.lower():
            continue
        visible = strip_think(buffer)
        if len(visible) > emitted:
            yield visible[emitted:]
            emitted = len(visible)


def split_into_chunks(text: str, size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    i = 0
    while i < len(text):
        end = min(len(text), i + size)
        if end < len(text):
            window = text[i:end]
            cut = max(window.rfind(". "), window.rfind("। "), window.rfind(" "))
            if cut > size * 0.6:
                end = i + cut + 1
        chunks.append(text[i:end].strip())
        if end >= len(text):
            break
        i = max(0, end - overlap)
    return chunks


async def full_notes(
    transcript: str, *, lang: str, on_progress=None, on_warning=None
) -> str:
    """Exhaustive notes covering the WHOLE video - this is what the PDF shows.

    Unlike the on-screen summary, nothing here is sampled away: the entire
    transcript is split into chunks and every chunk is written up in full. The
    output is meant to be long - a two-hour lecture legitimately produces
    dozens of pages.

    Three things protect completeness:
      * chunks are small (NOTES_CHUNK_CHARS) with an overlap, so the model has
        little reason to compress and no point falls between two chunks;
      * a chunk that fails is retried, and if it still fails the caller is told
        which part is missing - it is never dropped silently;
      * NOTES_MAX_CHUNKS defaults to 0 (no cap), and any cap that is set is
        reported too.
    """
    model = model_for(lang)
    directive = language_directive(lang)
    reminder = (
        ""
        if lang == "en"
        else f"\n\nFINAL REMINDER: write everything in {language_name(lang)} only."
    )

    chunks = split_into_chunks(
        transcript, settings.NOTES_CHUNK_CHARS, settings.NOTES_CHUNK_OVERLAP
    )
    if settings.NOTES_MAX_CHUNKS and len(chunks) > settings.NOTES_MAX_CHUNKS:
        dropped = len(chunks) - settings.NOTES_MAX_CHUNKS
        chunks = chunks[: settings.NOTES_MAX_CHUNKS]
        if on_warning:
            await on_warning(
                f"This video is very long: the last {dropped} section(s) were left "
                f"out because NOTES_MAX_CHUNKS is set to {settings.NOTES_MAX_CHUNKS}. "
                "Set it to 0 for no limit."
            )

    total = len(chunks)
    parts: list[str] = [""] * total
    failed: list[int] = []
    done = 0
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(max(1, settings.NOTES_CONCURRENCY))

    async def write_chunk(idx: int, chunk: str) -> None:
        nonlocal done
        base = NOTES_FIRST_PROMPT if idx == 0 else NOTES_SEGMENT_PROMPT
        text = ""
        async with semaphore:
            for attempt in range(max(1, settings.NOTES_CHUNK_RETRIES)):
                try:
                    text = await collect_chat(
                        model=model,
                        system=directive + "\n\n" + base + reminder,
                        content=chunk,
                        num_predict=settings.NOTES_NUM_PREDICT,
                    )
                    if text.strip():
                        break
                except Exception as exc:
                    logger.warning(
                        "notes chunk %s/%s attempt %s failed: %s",
                        idx + 1, total, attempt + 1, exc,
                    )
        parts[idx] = (text or "").strip()
        async with lock:
            done += 1
            if not parts[idx]:
                failed.append(idx + 1)
            if on_progress:
                await on_progress(done, total)

    await asyncio.gather(*(write_chunk(i, c) for i, c in enumerate(chunks)))

    if failed and on_warning:
        await on_warning(
            f"{len(failed)} of {total} sections could not be written "
            f"(part {', '.join(str(n) for n in sorted(failed))}). "
            "Everything else is included - try again to fill the gaps."
        )

    return "\n\n".join(p for p in parts if p).strip()
