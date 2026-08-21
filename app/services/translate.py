"""Translation via Google's free translate endpoint.

Same engine the Chrome extension already uses, so quality is identical and no
API key is needed. Text is chunked on line boundaries, which keeps the Markdown
structure (headings, bullets) intact through the round trip.

Used for two things:
  1. The "Translate to…" picker in the UI.
  2. An automatic safety net - if the model ignores the language instruction and
     answers in the wrong script, we translate the result instead of shipping a
     summary in a language the user did not ask for.
"""
from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger("trialguard.translate")

ENDPOINT = "https://translate.googleapis.com/translate_a/single"

# Scripts that identify a language (or a small family) on sight.
_SCRIPTS = {
    "deva": re.compile(r"[ऀ-ॿ]"),   # Hindi, Marathi, Nepali, Sanskrit
    "beng": re.compile(r"[ঀ-৿]"),   # Bengali, Assamese
    "guru": re.compile(r"[਀-੿]"),   # Punjabi
    "gujr": re.compile(r"[઀-૿]"),
    "orya": re.compile(r"[଀-୿]"),
    "taml": re.compile(r"[஀-௿]"),
    "telu": re.compile(r"[ఀ-౿]"),
    "knda": re.compile(r"[ಀ-೿]"),
    "mlym": re.compile(r"[ഀ-ൿ]"),
    "arab": re.compile(r"[؀-ۿ]"),   # Arabic, Urdu, Persian
    "cyrl": re.compile(r"[Ѐ-ӿ]"),
    "hani": re.compile(r"[一-鿿]"),
    "hang": re.compile(r"[가-힯]"),
    "kana": re.compile(r"[぀-ヿ]"),
    "hebr": re.compile(r"[֐-׿]"),
    "grek": re.compile(r"[Ͱ-Ͽ]"),
    "thai": re.compile(r"[฀-๿]"),
    "latn": re.compile(r"[A-Za-z]"),
}

LANG_SCRIPT = {
    "hi": "deva", "mr": "deva", "ne": "deva", "sa": "deva", "kok": "deva", "mai": "deva",
    "bn": "beng", "as": "beng",
    "pa": "guru", "gu": "gujr", "or": "orya", "ta": "taml", "te": "telu",
    "kn": "knda", "ml": "mlym",
    "ar": "arab", "ur": "arab", "fa": "arab", "sd": "arab",
    "ru": "cyrl", "uk": "cyrl",
    "zh": "hani", "ja": "kana", "ko": "hang",
    "he": "hebr", "el": "grek", "th": "thai",
}


def script_matches(text: str, lang: str, *, min_ratio: float = 0.15) -> bool:
    """Is `text` plausibly written in `lang`?

    Used to catch the failure mode where a model is told "write in Hindi" and
    answers in English anyway. Languages that share the Latin alphabet cannot be
    told apart this way, so those always pass - the check only rejects a clear
    script mismatch, never a plausible one.
    """
    sample = (text or "").strip()
    if len(sample) < 40:
        return True

    expected = LANG_SCRIPT.get(lang, "latn")
    pattern = _SCRIPTS.get(expected)
    if pattern is None:
        return True

    hits = len(pattern.findall(sample))
    letters = hits + len(_SCRIPTS["latn"].findall(sample))
    if letters == 0:
        return True

    if expected == "latn":
        # For a Latin-script target, only reject if another script dominates.
        for name, other in _SCRIPTS.items():
            if name == "latn":
                continue
            if len(other.findall(sample)) > letters * 0.3:
                return False
        return True

    return hits / letters >= min_ratio


def _chunk(text: str, size: int = 1500) -> list[str]:
    """Split on line boundaries so Markdown structure survives translation."""
    chunks: list[str] = []
    current = ""
    for line in str(text or "").split("\n"):
        if current and len(current) + 1 + len(line) > size:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks or [""]


async def translate(text: str, target: str) -> str:
    """Translate Markdown into `target`, preserving line structure."""
    if not text or not target:
        return text

    out: list[str] = []
    async with httpx.AsyncClient(timeout=45) as client:
        for chunk in _chunk(text):
            if not chunk.strip():
                out.append(chunk)
                continue
            res = await client.get(
                ENDPOINT,
                params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": chunk},
            )
            if res.status_code != 200:
                raise RuntimeError(f"Translate HTTP {res.status_code}")
            data = res.json()
            segments = data[0] if data and data[0] else []
            out.append("".join((s[0] or "") for s in segments if s))
    return "\n".join(out)


async def ensure_language(text: str, target: str) -> tuple[str, bool]:
    """Return (text, was_translated).

    If the text is already in the right script it is returned untouched. If the
    model answered in the wrong language we translate rather than shipping the
    wrong one - and if translation itself fails we return the original, because
    a summary in the wrong language beats no summary at all.
    """
    if not text or not target or script_matches(text, target):
        return text, False
    try:
        translated = await translate(text, target)
        if translated and len(translated.strip()) > 20:
            logger.info("auto-translated model output into %s", target)
            return translated, True
    except Exception as exc:  # pragma: no cover - network
        logger.warning("auto-translation into %s failed: %s", target, exc)
    return text, False
