import asyncio
import html
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

import aiohttp

LIBRETRANSLATE_TIMEOUT_SECONDS = 20
DEFAULT_LIBRETRANSLATE_ENDPOINT = "https://translate.argosopentech.com/translate"
MYMEMORY_ENDPOINT = "https://api.mymemory.translated.net/get"
ALLOWED_LANGUAGE_CODES = {"auto", "uz", "en", "ru", "zh-cn"}

LANG_LABELS = {
    "auto": "Auto",
    "uz": "Uzbek",
    "en": "English",
    "ru": "Russian",
    "zh-cn": "Chinese (Simplified)",
}


@dataclass(slots=True)
class TranslationResult:
    source: str
    target: str
    text: str
    pronunciation: str
    engine: str


def language_name(code: str) -> str:
    return LANG_LABELS.get(code.lower(), code.upper())


def _normalize_language(code: str) -> str:
    normalized = (code or "").strip().lower()
    if not normalized:
        return "auto"
    if normalized.startswith("zh"):
        return "zh-cn"
    return normalized


def _validate_language(code: str, *, allow_auto: bool) -> str:
    normalized = _normalize_language(code)
    if normalized == "auto" and allow_auto:
        return normalized
    if normalized == "auto" and not allow_auto:
        raise ValueError("Maqsad til uchun auto ishlatilmaydi.")
    if normalized not in ALLOWED_LANGUAGE_CODES:
        raise ValueError("Faqat uz, en, ru, zh-cn tillari qo'llab-quvvatlanadi.")
    return normalized


def _libre_code(code: str) -> str:
    normalized = _normalize_language(code)
    if normalized == "zh-cn":
        return "zh"
    return normalized


def _guess_source_language(text: str) -> str:
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            return "zh-cn"
    for ch in text:
        if "\u0400" <= ch <= "\u04ff":
            return "ru"
    return "en"


def _translate_google_sync(text: str, source: str, target: str) -> TranslationResult:
    try:
        from googletrans import Translator
    except Exception as error:  # noqa: BLE001
        raise RuntimeError(
            "googletrans kutubxonasi topilmadi. requirements ni yangilang."
        ) from error

    translator = Translator()
    result = translator.translate(text, src=source, dest=target)
    translated = str(getattr(result, "text", "") or "").strip()
    if not translated:
        raise RuntimeError("Googletrans tarjima javobi bo'sh qaytdi.")

    return TranslationResult(
        source=_normalize_language(str(getattr(result, "src", source) or source)),
        target=_normalize_language(str(getattr(result, "dest", target) or target)),
        text=html.unescape(translated),
        pronunciation=str(getattr(result, "pronunciation", "") or "").strip(),
        engine="google",
    )


def _detected_source_from_libre(payload: dict[str, Any], default_source: str) -> str:
    detected = payload.get("detectedLanguage")
    if isinstance(detected, dict):
        return _normalize_language(str(detected.get("language", default_source)))
    if isinstance(detected, str):
        return _normalize_language(detected)
    return _normalize_language(default_source)


async def _translate_libre(
    text: str, source: str, target: str, endpoint: str, api_key: str
) -> TranslationResult:
    timeout = aiohttp.ClientTimeout(total=LIBRETRANSLATE_TIMEOUT_SECONDS)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "text": text,
        "q": text,
        "source": _libre_code(source),
        "target": _libre_code(target),
        "format": "text",
    }
    if api_key:
        payload["api_key"] = api_key

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(endpoint, headers=headers, json=payload) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                message = ""
                if isinstance(body, dict):
                    message = str(body.get("message", "")).strip()
                raise RuntimeError(
                    f"LibreTranslate API xatosi: HTTP {response.status}. {message or 'Request failed'}"
                )

    if not isinstance(body, dict):
        raise RuntimeError("LibreTranslate API noto'g'ri formatda javob qaytardi.")

    translated = html.unescape(str(body.get("translatedText", "")).strip())
    if not translated:
        raise RuntimeError("LibreTranslate tarjima javobi bo'sh qaytdi.")

    detected_source = _detected_source_from_libre(body, source)
    detected_target = _normalize_language(target)
    return TranslationResult(
        source=detected_source,
        target=detected_target,
        text=translated,
        pronunciation="",
        engine="libretranslate",
    )


async def _translate_mymemory(
    text: str, source: str, target: str
) -> TranslationResult:
    source_code = _libre_code(source)
    if source_code == "auto":
        source_code = _libre_code(_guess_source_language(text))
    target_code = _libre_code(target)
    if source_code == target_code:
        return TranslationResult(
            source=_normalize_language(source_code),
            target=_normalize_language(target_code),
            text=text,
            pronunciation="",
            engine="mymemory",
        )
    langpair = f"{source_code}|{target_code}"
    params = {"q": text, "langpair": langpair}
    timeout = aiohttp.ClientTimeout(total=LIBRETRANSLATE_TIMEOUT_SECONDS)
    headers = {"Accept": "application/json, text/plain, */*"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(MYMEMORY_ENDPOINT, params=params) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(
                    f"MyMemory API xatosi: HTTP {response.status}. {quote_plus(text[:40])}"
                )

    if not isinstance(body, dict):
        raise RuntimeError("MyMemory API noto'g'ri formatda javob qaytardi.")
    status = int(body.get("responseStatus", 0) or 0)
    if status != 200:
        details = str(body.get("responseDetails", "")).strip()
        raise RuntimeError(f"MyMemory API xatosi: {details or status}")
    response_data = body.get("responseData")
    if not isinstance(response_data, dict):
        raise RuntimeError("MyMemory API responseData topilmadi.")
    translated = html.unescape(str(response_data.get("translatedText", "")).strip())
    if not translated:
        raise RuntimeError("MyMemory tarjima javobi bo'sh qaytdi.")

    detected = _normalize_language(source if source != "auto" else source_code)
    return TranslationResult(
        source=detected,
        target=_normalize_language(target),
        text=translated,
        pronunciation="",
        engine="mymemory",
    )


def _clean_text(text: str) -> str:
    clean_text = (text or "").strip()
    if not clean_text:
        raise ValueError("Tarjima uchun matn yuboring.")
    if len(clean_text) > 5000:
        raise ValueError("Matn juda uzun. Maksimal 5000 belgi.")
    return clean_text


async def translate_text(
    text: str, source: str, target: str, engine: str = "auto"
) -> TranslationResult:
    clean_text = _clean_text(text)
    normalized_source = _validate_language(source, allow_auto=True)
    normalized_target = _validate_language(target, allow_auto=False)
    normalized_engine = (engine or "auto").strip().lower()
    if normalized_engine in {"libre", "libretranslate"}:
        normalized_engine = "libretranslate"
    if normalized_engine not in {"auto", "google", "libretranslate"}:
        raise ValueError("Tarjima engine noto'g'ri berildi.")

    libre_endpoint = (
        os.getenv("LIBRETRANSLATE_ENDPOINT", "").strip()
        or DEFAULT_LIBRETRANSLATE_ENDPOINT
    )
    libre_api_key = os.getenv("LIBRETRANSLATE_API_KEY", "").strip()

    if normalized_engine == "google":
        return await asyncio.to_thread(
            _translate_google_sync,
            clean_text,
            normalized_source,
            normalized_target,
        )

    if normalized_engine == "libretranslate":
        try:
            return await _translate_libre(
                clean_text,
                normalized_source,
                normalized_target,
                libre_endpoint,
                libre_api_key,
            )
        except Exception:
            return await _translate_mymemory(
                clean_text,
                normalized_source,
                normalized_target,
            )

    try:
        return await _translate_libre(
            clean_text,
            normalized_source,
            normalized_target,
            libre_endpoint,
            libre_api_key,
        )
    except Exception:  # noqa: BLE001
        try:
            return await _translate_mymemory(
                clean_text,
                normalized_source,
                normalized_target,
            )
        except Exception:
            pass

    try:
        return await asyncio.to_thread(
            _translate_google_sync,
            clean_text,
            normalized_source,
            normalized_target,
        )
    except Exception:
        return await _translate_mymemory(
            clean_text,
            normalized_source,
            normalized_target,
        )
