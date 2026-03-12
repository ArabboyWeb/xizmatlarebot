from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


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


_TOKEN_OVERRIDES_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "token_tariffs.json"
)
_OVERRIDE_CACHE: dict[str, dict[str, int]] = {}
_OVERRIDE_MTIME_NS = -1
_ECONOMY_SETTINGS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "economy_settings.json"
)
_ECONOMY_CACHE: dict[str, int] = {}
_ECONOMY_MTIME_NS = -1


def _economy_defaults() -> dict[str, int]:
    return {
        "referral_inviter_bonus": max(1, _read_int("BOT_REFERRAL_INVITER_BONUS", 40)),
        "referral_invitee_bonus": max(1, _read_int("BOT_REFERRAL_INVITEE_BONUS", 20)),
    }


_ECONOMY_MINIMUMS: dict[str, int] = {
    "referral_inviter_bonus": 1,
    "referral_invitee_bonus": 1,
}


def _normalize_economy_payload(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, int] = {}
    defaults = _economy_defaults()
    for key in defaults:
        if key not in raw:
            continue
        try:
            value = int(raw[key])
        except (TypeError, ValueError):
            continue
        normalized[key] = max(_ECONOMY_MINIMUMS[key], value)
    return normalized


def _load_economy_overrides() -> dict[str, int]:
    global _ECONOMY_CACHE, _ECONOMY_MTIME_NS
    if not _ECONOMY_SETTINGS_PATH.exists():
        _ECONOMY_CACHE = {}
        _ECONOMY_MTIME_NS = -1
        return {}
    try:
        stat = _ECONOMY_SETTINGS_PATH.stat()
        mtime_ns = int(stat.st_mtime_ns)
    except OSError:
        _ECONOMY_CACHE = {}
        _ECONOMY_MTIME_NS = -1
        return {}
    if mtime_ns == _ECONOMY_MTIME_NS:
        return dict(_ECONOMY_CACHE)
    try:
        payload = json.loads(_ECONOMY_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    normalized = _normalize_economy_payload(payload)
    _ECONOMY_CACHE = dict(normalized)
    _ECONOMY_MTIME_NS = mtime_ns
    return normalized


def _save_economy_overrides(overrides: dict[str, int]) -> None:
    global _ECONOMY_CACHE, _ECONOMY_MTIME_NS
    _ECONOMY_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _ECONOMY_SETTINGS_PATH.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(_ECONOMY_SETTINGS_PATH)
    try:
        _ECONOMY_MTIME_NS = int(_ECONOMY_SETTINGS_PATH.stat().st_mtime_ns)
    except OSError:
        _ECONOMY_MTIME_NS = -1
    _ECONOMY_CACHE = dict(overrides)


def economy_settings() -> dict[str, int]:
    settings = _economy_defaults()
    settings.update(_load_economy_overrides())
    return settings


def set_economy_setting(name: str, value: int) -> dict[str, int]:
    key = str(name or "").strip().lower()
    defaults = _economy_defaults()
    if key not in defaults:
        raise KeyError(f"Setting topilmadi: {name}")
    normalized_value = max(_ECONOMY_MINIMUMS[key], int(value))
    overrides = _load_economy_overrides()
    if normalized_value == defaults[key]:
        overrides.pop(key, None)
    else:
        overrides[key] = normalized_value
    _save_economy_overrides(overrides)
    return economy_settings()


def reset_economy_setting(name: str) -> dict[str, int]:
    key = str(name or "").strip().lower()
    defaults = _economy_defaults()
    if key not in defaults:
        raise KeyError(f"Setting topilmadi: {name}")
    overrides = _load_economy_overrides()
    overrides.pop(key, None)
    _save_economy_overrides(overrides)
    return economy_settings()


def _normalize_override_payload(raw: object) -> dict[str, dict[str, int]]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, int]] = {}
    for key, value in raw.items():
        service_key = str(key or "").strip().lower()
        if service_key not in SERVICE_TARIFFS:
            continue
        if not isinstance(value, dict):
            continue
        free_cost = value.get("free_cost")
        premium_cost = value.get("premium_cost")
        try:
            free_value = max(1, int(free_cost))
            premium_value = max(1, int(premium_cost))
        except (TypeError, ValueError):
            continue
        normalized[service_key] = {
            "free_cost": free_value,
            "premium_cost": premium_value,
        }
    return normalized


def _load_overrides() -> dict[str, dict[str, int]]:
    global _OVERRIDE_CACHE, _OVERRIDE_MTIME_NS
    if not _TOKEN_OVERRIDES_PATH.exists():
        _OVERRIDE_CACHE = {}
        _OVERRIDE_MTIME_NS = -1
        return {}
    try:
        stat = _TOKEN_OVERRIDES_PATH.stat()
        mtime_ns = int(stat.st_mtime_ns)
    except OSError:
        _OVERRIDE_CACHE = {}
        _OVERRIDE_MTIME_NS = -1
        return {}
    if mtime_ns == _OVERRIDE_MTIME_NS:
        return dict(_OVERRIDE_CACHE)
    try:
        payload = json.loads(_TOKEN_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    normalized = _normalize_override_payload(payload)
    _OVERRIDE_CACHE = dict(normalized)
    _OVERRIDE_MTIME_NS = mtime_ns
    return normalized


def _save_overrides(overrides: dict[str, dict[str, int]]) -> None:
    global _OVERRIDE_CACHE, _OVERRIDE_MTIME_NS
    _TOKEN_OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _TOKEN_OVERRIDES_PATH.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(_TOKEN_OVERRIDES_PATH)
    try:
        _OVERRIDE_MTIME_NS = int(_TOKEN_OVERRIDES_PATH.stat().st_mtime_ns)
    except OSError:
        _OVERRIDE_MTIME_NS = -1
    _OVERRIDE_CACHE = dict(overrides)


def normalize_plan(plan: str) -> str:
    normalized = (plan or "free").strip().lower()
    return "premium" if normalized == "premium" else "free"


def free_reset_tokens() -> int:
    return free_daily_tokens()


def free_reset_hours() -> int:
    return refill_interval_hours()


def free_daily_tokens() -> int:
    return 20


def free_signup_tokens() -> int:
    return 100


def premium_daily_tokens() -> int:
    return 100


def premium_upgrade_tokens() -> int:
    return 1000


def refill_interval_hours() -> int:
    return 24


def premium_price_uzs() -> int:
    return 15_000


def premium_card_number() -> str:
    return "5614 6812 8153 6858"


def referral_inviter_bonus() -> int:
    return int(economy_settings()["referral_inviter_bonus"])


def referral_invitee_bonus() -> int:
    return int(economy_settings()["referral_invitee_bonus"])


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


LEGACY_SERVICE_KEY_ALIASES: dict[str, str] = {
    "ai": "ai_chat",
    "currency": "currency_refresh",
    "download:direct": "save_direct",
    "download:instagram_video": "social_download",
    "download:tiktok_video": "social_download",
    "download:youtube": "youtube_download_video",
    "download:youtube_audio": "youtube_download_audio",
    "download:youtube_video": "youtube_download_video",
    "jobs": "jobs_search",
    "pollinations": "pollinations_generate",
    "save": "save_direct",
    "translate": "translate_text",
    "weather": "weather_lookup",
    "wikipedia": "wikipedia_search",
    "youtube": "youtube_search",
}


def resolve_service_key(service_key: str) -> str:
    key = str(service_key or "").strip().lower()
    if not key:
        return ""
    return LEGACY_SERVICE_KEY_ALIASES.get(key, key)


def service_tariff(service_key: str) -> ServiceTariff:
    key = resolve_service_key(service_key)
    if key not in SERVICE_TARIFFS:
        raise KeyError(f"Tariff topilmadi: {service_key}")
    base = SERVICE_TARIFFS[key]
    override = _load_overrides().get(key, {})
    free_cost = int(override.get("free_cost", base.free_cost))
    premium_cost = int(override.get("premium_cost", base.premium_cost))
    return ServiceTariff(
        key=base.key,
        label=base.label,
        category=base.category,
        free_cost=max(1, free_cost),
        premium_cost=max(1, premium_cost),
        description=base.description,
    )


def service_cost(service_key: str, *, plan: str) -> int:
    tariff = service_tariff(service_key)
    if normalize_plan(plan) == "premium":
        return tariff.premium_cost
    return tariff.free_cost


def list_tariffs(*, category: str = "") -> list[ServiceTariff]:
    normalized = str(category or "").strip().lower()
    services: list[ServiceTariff] = []
    for key in sorted(SERVICE_TARIFFS.keys()):
        tariff = service_tariff(key)
        if normalized and tariff.category != normalized:
            continue
        services.append(tariff)
    return services


def tariff_categories() -> list[str]:
    categories = {item.category for item in SERVICE_TARIFFS.values()}
    return sorted(categories)


def set_service_tariff_cost(
    service_key: str,
    *,
    free_cost: int | None = None,
    premium_cost: int | None = None,
) -> ServiceTariff:
    key = resolve_service_key(service_key)
    if key not in SERVICE_TARIFFS:
        raise KeyError(f"Tariff topilmadi: {service_key}")
    if free_cost is None and premium_cost is None:
        return service_tariff(key)

    current = service_tariff(key)
    updated_free = current.free_cost if free_cost is None else max(1, int(free_cost))
    updated_premium = (
        current.premium_cost if premium_cost is None else max(1, int(premium_cost))
    )

    overrides = _load_overrides()
    defaults = SERVICE_TARIFFS[key]
    if (
        updated_free == defaults.free_cost
        and updated_premium == defaults.premium_cost
    ):
        overrides.pop(key, None)
    else:
        overrides[key] = {
            "free_cost": updated_free,
            "premium_cost": updated_premium,
        }
    _save_overrides(overrides)
    return service_tariff(key)


def reset_service_tariff(service_key: str) -> ServiceTariff:
    key = resolve_service_key(service_key)
    if key not in SERVICE_TARIFFS:
        raise KeyError(f"Tariff topilmadi: {service_key}")
    overrides = _load_overrides()
    overrides.pop(key, None)
    _save_overrides(overrides)
    return service_tariff(key)
