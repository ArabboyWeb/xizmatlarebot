import html
from dataclasses import dataclass
from typing import Any

from services.rapidapi_client import rapidapi_post_form
from services.translate_client import translate_text as free_translate_text

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
        translated = html.unescape(str(data.get("translatedText", "")).strip())
        if translated:
            return translated
    result = payload.get("translatedText")
    if isinstance(result, str) and result.strip():
        return html.unescape(result.strip())
    return ""


def _fallback_language(code: str) -> str:
    normalized = _normalize_lang(code)
    if normalized == "zh":
        return "zh-cn"
    return normalized


def _result_language(code: str) -> str:
    normalized = _normalize_lang(code)
    if normalized.startswith("zh"):
        return "zh"
    return normalized


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

    rapidapi_error: Exception | None = None
    try:
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
        if translated:
            return TranslationResult(source=src, target=dst, text=translated)
    except Exception as error:  # noqa: BLE001
        rapidapi_error = error

    try:
        fallback_result = await free_translate_text(
            clean_text,
            _fallback_language(src),
            _fallback_language(dst),
        )
        return TranslationResult(
            source=_result_language(fallback_result.source),
            target=_result_language(fallback_result.target),
            text=html.unescape(fallback_result.text),
        )
    except Exception as fallback_error:  # noqa: BLE001
        if rapidapi_error is not None:
            raise RuntimeError("Tarjima xizmati vaqtincha ishlamayapti.") from fallback_error
        raise RuntimeError("Tarjima natijasi bo'sh qaytdi.") from fallback_error
