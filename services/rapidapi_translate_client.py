from dataclasses import dataclass
from typing import Any

from services.rapidapi_client import rapidapi_post_form

TRANSLATOR_HOST = "text-translator2.p.rapidapi.com"
TRANSLATOR_URL = "https://text-translator2.p.rapidapi.com/translate"
ALLOWED_LANGS = {"uz", "en", "ru", "zh"}

LANG_LABELS = {
    "uz": "Uzbek",
    "en": "English",
    "ru": "Russian",
    "zh": "Chinese",
}


@dataclass(slots=True)
class TranslationResult:
    source: str
    target: str
    text: str


def language_name(code: str) -> str:
    return LANG_LABELS.get(code.lower(), code.upper())


def _normalize_lang(code: str) -> str:
    normalized = (code or "").strip().lower()
    if normalized.startswith("zh"):
        return "zh"
    return normalized


def _extract_text(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        translated = str(data.get("translatedText", "")).strip()
        if translated:
            return translated
    result = payload.get("translatedText")
    if isinstance(result, str) and result.strip():
        return result.strip()
    return ""


async def translate_text(text: str, source: str, target: str) -> TranslationResult:
    clean_text = (text or "").strip()
    if not clean_text:
        raise ValueError("Tarjima uchun matn yuboring.")
    if len(clean_text) > 5000:
        raise ValueError("Matn juda uzun. Maksimal 5000 belgi.")

    src = _normalize_lang(source)
    dst = _normalize_lang(target)
    if src not in ALLOWED_LANGS:
        raise ValueError("Source til noto'g'ri.")
    if dst not in ALLOWED_LANGS:
        raise ValueError("Target til noto'g'ri.")
    if src == dst:
        return TranslationResult(source=src, target=dst, text=clean_text)

    payload = await rapidapi_post_form(
        host=TRANSLATOR_HOST,
        url=TRANSLATOR_URL,
        data={
            "source_language": src,
            "target_language": dst,
            "text": clean_text,
        },
    )
    translated = _extract_text(payload)
    if not translated:
        raise RuntimeError("Tarjima natijasi bo'sh qaytdi.")

    return TranslationResult(source=src, target=dst, text=translated)
