from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ServiceTariff:
    key: str
    label: str
    category: str
    free_cost: int
    premium_cost: int
    description: str


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def normalize_plan(plan: str) -> str:
    normalized = (plan or "free").strip().lower()
    return "premium" if normalized == "premium" else "free"


def free_daily_tokens() -> int:
    return max(
        20,
        _read_int("BOT_FREE_DAILY_TOKENS", _read_int("AI_FREE_DAILY_REQUESTS", 40)),
    )


def premium_monthly_tokens() -> int:
    return max(
        800,
        _read_int(
            "BOT_PREMIUM_MONTHLY_TOKENS",
            _read_int("AI_PREMIUM_MONTHLY_CREDITS", 1800),
        ),
    )


def referral_inviter_bonus() -> int:
    return max(10, _read_int("BOT_REFERRAL_INVITER_BONUS", 40))


def referral_invitee_bonus() -> int:
    return max(5, _read_int("BOT_REFERRAL_INVITEE_BONUS", 20))


def ai_min_cost(plan: str) -> int:
    if normalize_plan(plan) == "premium":
        return max(3, _read_int("BOT_AI_PREMIUM_MIN_COST", 5))
    return max(4, _read_int("BOT_AI_FREE_MIN_COST", 8))


SERVICE_TARIFFS: dict[str, ServiceTariff] = {
    "ai_chat": ServiceTariff(
        key="ai_chat",
        label="AI Chat",
        category="ai",
        free_cost=8,
        premium_cost=5,
        description="AI matnli muloqot",
    ),
    "currency_refresh": ServiceTariff(
        key="currency_refresh",
        label="Valyuta kursi",
        category="lookup",
        free_cost=1,
        premium_cost=1,
        description="CBU kurslarini yangilash",
    ),
    "weather_lookup": ServiceTariff(
        key="weather_lookup",
        label="Ob-havo",
        category="lookup",
        free_cost=2,
        premium_cost=1,
        description="Shahar yoki lokatsiya bo'yicha ob-havo",
    ),
    "translate_text": ServiceTariff(
        key="translate_text",
        label="Tarjimon",
        category="lookup",
        free_cost=3,
        premium_cost=2,
        description="Matn tarjimasi",
    ),
    "tinyurl_create": ServiceTariff(
        key="tinyurl_create",
        label="Link qisqartirish",
        category="lookup",
        free_cost=2,
        premium_cost=1,
        description="URL qisqartirish",
    ),
    "jobs_search": ServiceTariff(
        key="jobs_search",
        label="Ish qidirish",
        category="lookup",
        free_cost=4,
        premium_cost=3,
        description="Vakansiya qidiruvi",
    ),
    "shazam_search": ServiceTariff(
        key="shazam_search",
        label="Musiqa qidirish",
        category="lookup",
        free_cost=4,
        premium_cost=3,
        description="Qo'shiq autocomplete va top tracklar",
    ),
    "wikipedia_search": ServiceTariff(
        key="wikipedia_search",
        label="Wikipedia",
        category="lookup",
        free_cost=2,
        premium_cost=1,
        description="Qisqa ensiklopediya ma'lumoti",
    ),
    "tempmail_new": ServiceTariff(
        key="tempmail_new",
        label="Yangi temp mail",
        category="utility",
        free_cost=2,
        premium_cost=1,
        description="Yangi disposable email yaratish",
    ),
    "tempmail_inbox": ServiceTariff(
        key="tempmail_inbox",
        label="Temp mail inbox",
        category="utility",
        free_cost=1,
        premium_cost=1,
        description="Inboxni yangilash",
    ),
    "tempmail_read": ServiceTariff(
        key="tempmail_read",
        label="Temp mail xabar o'qish",
        category="utility",
        free_cost=1,
        premium_cost=1,
        description="Inbox xabarini ochish",
    ),
    "save_direct": ServiceTariff(
        key="save_direct",
        label="Fayl saqlash",
        category="media",
        free_cost=6,
        premium_cost=4,
        description="Direct fayl yuklash",
    ),
    "save_youtube_video": ServiceTariff(
        key="save_youtube_video",
        label="YouTube video",
        category="media",
        free_cost=12,
        premium_cost=9,
        description="YouTube video yuklash",
    ),
    "save_youtube_audio": ServiceTariff(
        key="save_youtube_audio",
        label="YouTube audio",
        category="media",
        free_cost=10,
        premium_cost=7,
        description="YouTube audio yuklash",
    ),
    "save_social_video": ServiceTariff(
        key="save_social_video",
        label="Instagram/TikTok video",
        category="media",
        free_cost=10,
        premium_cost=8,
        description="Social video yuklash",
    ),
    "youtube_search": ServiceTariff(
        key="youtube_search",
        label="YouTube qidiruv",
        category="media",
        free_cost=3,
        premium_cost=2,
        description="YouTube natija qidiruvi",
    ),
    "youtube_download_video": ServiceTariff(
        key="youtube_download_video",
        label="YouTube video yuklash",
        category="media",
        free_cost=12,
        premium_cost=9,
        description="YouTube section video download",
    ),
    "youtube_download_audio": ServiceTariff(
        key="youtube_download_audio",
        label="YouTube audio yuklash",
        category="media",
        free_cost=10,
        premium_cost=7,
        description="YouTube section audio download",
    ),
    "social_download": ServiceTariff(
        key="social_download",
        label="Instagram/TikTok download",
        category="media",
        free_cost=10,
        premium_cost=8,
        description="YouTube section ichidagi social download",
    ),
    "pollinations_generate": ServiceTariff(
        key="pollinations_generate",
        label="AI rasm yaratish",
        category="ai",
        free_cost=20,
        premium_cost=16,
        description="AI image generation",
    ),
    "converter_word_to_pdf": ServiceTariff(
        key="converter_word_to_pdf",
        label="Word -> PDF",
        category="productivity",
        free_cost=8,
        premium_cost=6,
        description="Word faylni PDF ga o'tkazish",
    ),
    "converter_pdf_to_word": ServiceTariff(
        key="converter_pdf_to_word",
        label="PDF -> Word",
        category="productivity",
        free_cost=10,
        premium_cost=8,
        description="PDF faylni DOCX ga o'tkazish",
    ),
    "converter_image_to_pdf": ServiceTariff(
        key="converter_image_to_pdf",
        label="Image -> PDF",
        category="productivity",
        free_cost=8,
        premium_cost=6,
        description="Rasmni PDF ga o'tkazish",
    ),
    "converter_pdf_to_images": ServiceTariff(
        key="converter_pdf_to_images",
        label="PDF -> Images",
        category="productivity",
        free_cost=12,
        premium_cost=10,
        description="PDF sahifalarni rasmlarga ajratish",
    ),
    "converter_image_format": ServiceTariff(
        key="converter_image_format",
        label="Image format",
        category="productivity",
        free_cost=6,
        premium_cost=5,
        description="Rasm formatini almashtirish",
    ),
}


def service_tariff(service_key: str) -> ServiceTariff:
    key = str(service_key or "").strip().lower()
    if key not in SERVICE_TARIFFS:
        raise KeyError(f"Tariff topilmadi: {service_key}")
    return SERVICE_TARIFFS[key]


def service_cost(service_key: str, *, plan: str) -> int:
    tariff = service_tariff(service_key)
    if normalize_plan(plan) == "premium":
        return tariff.premium_cost
    return tariff.free_cost
