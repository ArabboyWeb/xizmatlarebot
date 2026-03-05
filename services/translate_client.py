import asyncio
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

DEEPLX_TIMEOUT_SECONDS = 20

LANG_LABELS = {
    "auto": "Auto",
    "uz": "Uzbek",
    "en": "English",
    "ru": "Russian",
    "tr": "Turkish",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "ar": "Arabic",
    "hi": "Hindi",
    "ja": "Japanese",
    "ko": "Korean",
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
    if normalized in {"zh", "zh-cn"}:
        return "zh-cn"
    return normalized


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
        text=translated,
        pronunciation=str(getattr(result, "pronunciation", "") or "").strip(),
        engine="google",
    )


def _extract_deeplx_text(payload: dict[str, Any]) -> str:
    direct = str(payload.get("data", "")).strip()
    if direct:
        return direct

    alternatives = payload.get("alternatives")
    if isinstance(alternatives, list) and alternatives:
        first = str(alternatives[0]).strip()
        if first:
            return first

    translations = payload.get("translations")
    if isinstance(translations, list) and translations:
        first = translations[0]
        if isinstance(first, dict):
            candidate = str(first.get("text", "")).strip()
            if candidate:
                return candidate

    return ""


async def _translate_deeplx(
    text: str, source: str, target: str, endpoint: str, auth_key: str
) -> TranslationResult:
    timeout = aiohttp.ClientTimeout(total=DEEPLX_TIMEOUT_SECONDS)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if auth_key:
        headers["Authorization"] = f"Bearer {auth_key}"

    source_lang = source if source != "auto" else "auto"
    target_lang = target
    payload = {
        "text": text,
        "source_lang": source_lang.upper() if source_lang != "auto" else "auto",
        "target_lang": target_lang.upper(),
    }

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(endpoint, headers=headers, json=payload) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                message = ""
                if isinstance(body, dict):
                    message = str(body.get("message", "")).strip()
                raise RuntimeError(
                    f"DeepLX API xatosi: HTTP {response.status}. {message or 'Request failed'}"
                )

    if not isinstance(body, dict):
        raise RuntimeError("DeepLX API noto'g'ri formatda javob qaytardi.")

    translated = _extract_deeplx_text(body)
    if not translated:
        raise RuntimeError("DeepLX tarjima javobi bo'sh qaytdi.")

    detected_source = _normalize_language(str(body.get("source_lang", source) or source))
    detected_target = _normalize_language(str(body.get("target_lang", target) or target))
    return TranslationResult(
        source=detected_source,
        target=detected_target,
        text=translated,
        pronunciation="",
        engine="deeplx",
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
    normalized_source = _normalize_language(source)
    normalized_target = _normalize_language(target)
    normalized_engine = (engine or "auto").strip().lower()
    if normalized_engine not in {"auto", "google", "deeplx"}:
        raise ValueError("Tarjima engine noto'g'ri berildi.")

    deeplx_endpoint = os.getenv("DEEPLX_ENDPOINT", "").strip()
    deeplx_auth_key = os.getenv("DEEPLX_AUTH_KEY", "").strip()

    if normalized_engine == "google":
        return await asyncio.to_thread(
            _translate_google_sync,
            clean_text,
            normalized_source,
            normalized_target,
        )

    if normalized_engine == "deeplx":
        if not deeplx_endpoint:
            raise RuntimeError(
                "DeepLX endpoint sozlanmagan. DEEPLX_ENDPOINT ni .env ga yozing."
            )
        return await _translate_deeplx(
            clean_text,
            normalized_source,
            normalized_target,
            deeplx_endpoint,
            deeplx_auth_key,
        )

    if deeplx_endpoint:
        try:
            return await _translate_deeplx(
                clean_text,
                normalized_source,
                normalized_target,
                deeplx_endpoint,
                deeplx_auth_key,
            )
        except Exception:  # noqa: BLE001
            pass

    return await asyncio.to_thread(
        _translate_google_sync,
        clean_text,
        normalized_source,
        normalized_target,
    )
