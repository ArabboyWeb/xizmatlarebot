from __future__ import annotations

import asyncio
import html
import os
from typing import Iterable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from services.analytics_store import AnalyticsStore
from services.token_pricing import (
    ServiceTariff,
    economy_settings,
    list_tariffs,
    reset_economy_setting,
    reset_service_tariff,
    service_tariff,
    set_economy_setting,
    set_service_tariff_cost,
    tariff_categories,
)

router = Router(name="admin")
DEFAULT_ADMIN_IDS = {1392745444}


class AdminState(StatesGroup):
    waiting_broadcast_message = State()


TOKEN_CATEGORY_LABELS = {
    "ai": "AI",
    "lookup": "Lookup",
    "media": "Media",
    "productivity": "Productivity",
    "utility": "Utility",
}

ECONOMY_SETTING_LABELS = {
    "referral_inviter_bonus": "Taklif qiluvchiga bonus",
    "referral_invitee_bonus": "Yangi user bonusi",
    "free_reset_tokens": "Free refill token",
    "free_reset_hours": "Free refill soat",
}

ECONOMY_SETTING_DELTAS = {
    "referral_inviter_bonus": (-10, -5, 5, 10),
    "referral_invitee_bonus": (-10, -5, 5, 10),
    "free_reset_tokens": (-10, -5, 5, 10),
    "free_reset_hours": (-6, -1, 1, 6),
}


def admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_USER_IDS", "").strip()
    values: set[int] = set(DEFAULT_ADMIN_IDS)
    if not raw:
        return values
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError:
            continue
    return values


def is_admin_user_id(user_id: int | None) -> bool:
    return isinstance(user_id, int) and user_id in admin_ids()


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Statistika", callback_data="admin:stats"),
                InlineKeyboardButton(text="Foydalanuvchilar", callback_data="admin:users"),
            ],
            [
                InlineKeyboardButton(text="Token tariflar", callback_data="admin:tokens"),
                InlineKeyboardButton(
                    text="Referral & reset",
                    callback_data="admin:economy",
                ),
            ],
            [InlineKeyboardButton(text="Reklama yuborish", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="Yangilash", callback_data="admin:panel")],
        ]
    )


def admin_broadcast_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Bekor qilish", callback_data="admin:panel")]
        ]
    )


def admin_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yuborish", callback_data="admin:broadcast:send")],
            [InlineKeyboardButton(text="Bekor qilish", callback_data="admin:broadcast:cancel")],
        ]
    )


def _token_categories_keyboard() -> InlineKeyboardMarkup:
    categories = tariff_categories()
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for category in categories:
        label = TOKEN_CATEGORY_LABELS.get(category, category.title())
        current_row.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"admin:tokens:cat:{category}",
            )
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append([InlineKeyboardButton(text="Orqaga", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _token_services_keyboard(category: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for tariff in list_tariffs(category=category):
        current_row.append(
            InlineKeyboardButton(
                text=tariff.label,
                callback_data=f"admin:tokens:svc:{tariff.key}",
            )
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append(
        [InlineKeyboardButton(text="Kategoriyalar", callback_data="admin:tokens")]
    )
    rows.append([InlineKeyboardButton(text="Orqaga", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _token_adjust_keyboard(tariff: ServiceTariff) -> InlineKeyboardMarkup:
    key = tariff.key
    rows = [
        [
            InlineKeyboardButton(
                text="Free -5",
                callback_data=f"admin:tokens:adj:{key}:free:-5",
            ),
            InlineKeyboardButton(
                text="Free -1",
                callback_data=f"admin:tokens:adj:{key}:free:-1",
            ),
            InlineKeyboardButton(
                text="Free +1",
                callback_data=f"admin:tokens:adj:{key}:free:1",
            ),
            InlineKeyboardButton(
                text="Free +5",
                callback_data=f"admin:tokens:adj:{key}:free:5",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Premium -5",
                callback_data=f"admin:tokens:adj:{key}:premium:-5",
            ),
            InlineKeyboardButton(
                text="Premium -1",
                callback_data=f"admin:tokens:adj:{key}:premium:-1",
            ),
            InlineKeyboardButton(
                text="Premium +1",
                callback_data=f"admin:tokens:adj:{key}:premium:1",
            ),
            InlineKeyboardButton(
                text="Premium +5",
                callback_data=f"admin:tokens:adj:{key}:premium:5",
            ),
        ],
        [
            InlineKeyboardButton(
                text="Defaultga qaytarish",
                callback_data=f"admin:tokens:reset:{key}",
            )
        ],
    ]
    category = str(tariff.category or "").strip().lower()
    if category:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Kategoriyaga qaytish",
                    callback_data=f"admin:tokens:cat:{category}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Admin panel", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _token_service_key_from_callback(callback_data: str) -> str:
    prefix = "admin:tokens:svc:"
    raw = str(callback_data or "").strip()
    if not raw.startswith(prefix):
        return ""
    payload = raw[len(prefix) :].strip().lower()
    if not payload:
        return ""
    with_category, _, maybe_category = payload.rpartition(":")
    if with_category and maybe_category in set(tariff_categories()):
        try:
            service_tariff(with_category)
            return with_category
        except KeyError:
            pass
    return payload


def _token_adjust_payload_from_callback(
    callback_data: str,
) -> tuple[str, str, int] | None:
    prefix = "admin:tokens:adj:"
    raw = str(callback_data or "").strip()
    if not raw.startswith(prefix):
        return None
    payload = raw[len(prefix) :].strip().lower()
    if not payload:
        return None
    parts = payload.rsplit(":", 2)
    if len(parts) != 3:
        return None
    key, plan_key, delta_raw = parts
    if not key or not plan_key or not delta_raw:
        return None
    try:
        return key, plan_key, int(delta_raw)
    except ValueError:
        return None


def _token_reset_key_from_callback(callback_data: str) -> str:
    prefix = "admin:tokens:reset:"
    raw = str(callback_data or "").strip()
    if not raw.startswith(prefix):
        return ""
    return raw[len(prefix) :].strip().lower()


def _economy_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for key in ECONOMY_SETTING_LABELS:
        current_row.append(
            InlineKeyboardButton(
                text=ECONOMY_SETTING_LABELS[key],
                callback_data=f"admin:economy:item:{key}",
            )
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append([InlineKeyboardButton(text="Admin panel", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _economy_item_keyboard(key: str) -> InlineKeyboardMarkup:
    deltas = ECONOMY_SETTING_DELTAS.get(key, (-1, 1))
    rows = [
        [
            InlineKeyboardButton(
                text=f"{delta:+d}",
                callback_data=f"admin:economy:adj:{key}:{delta}",
            )
            for delta in deltas
        ],
        [
            InlineKeyboardButton(
                text="Defaultga qaytarish",
                callback_data=f"admin:economy:reset:{key}",
            )
        ],
        [InlineKeyboardButton(text="Referral & reset", callback_data="admin:economy")],
        [InlineKeyboardButton(text="Admin panel", callback_data="admin:panel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fmt_service_name(key: str) -> str:
    mapping = {
        "ai": "Sun'iy Intellekt",
        "save": "Saqlash",
        "weather": "Ob-havo",
        "currency": "Valyuta",
        "converter": "Konvertor",
        "tempmail": "Vaqtinchalik pochta",
        "tinyurl": "Link qisqartirish",
        "shazam": "Musiqa qidirish",
        "translate": "Tarjimon",
        "jobs": "Ish qidirish",
        "youtube": "YouTube",
        "wikipedia": "Wikipedia",
        "pollinations": "Rasm yaratish",
        "download:direct": "Direct yuklash",
        "download:youtube": "YouTube yuklash",
        "download:youtube_video": "YouTube video yuklash",
        "download:youtube_audio": "YouTube audio yuklash",
        "download:instagram_video": "Instagram video yuklash",
        "download:tiktok_video": "TikTok video yuklash",
    }
    return mapping.get(key, key.replace("_", " ").title())


def _token_overview_text() -> str:
    rows = [
        "<b>Token tariflar paneli</b>",
        "Har bir servis uchun Free/Premium token sarfini shu yerdan o'zgartiring.",
        "",
    ]
    for category in tariff_categories():
        label = TOKEN_CATEGORY_LABELS.get(category, category.title())
        services = list_tariffs(category=category)
        rows.append(f"<b>{html.escape(label)}</b>: {len(services)} ta servis")
    rows.append("")
    rows.append("Kategoriyani tanlang.")
    return "\n".join(rows)


def _token_category_text(category: str) -> str:
    normalized = str(category or "").strip().lower()
    label = TOKEN_CATEGORY_LABELS.get(normalized, normalized.title() or "Kategoriya")
    rows = [f"<b>{html.escape(label)} tariflari</b>", ""]
    services = list_tariffs(category=normalized)
    if not services:
        rows.append("Servis topilmadi.")
    else:
        for tariff in services:
            rows.append(
                f"- <b>{html.escape(tariff.label)}</b> ({html.escape(tariff.key)}): "
                f"Free <b>{tariff.free_cost}</b> / Premium <b>{tariff.premium_cost}</b>"
            )
    rows.append("")
    rows.append("Tahrirlash uchun servis tugmasini bosing.")
    return "\n".join(rows)


def _token_service_text(tariff: ServiceTariff) -> str:
    category = TOKEN_CATEGORY_LABELS.get(tariff.category, tariff.category.title())
    return (
        "<b>Token tarif tahriri</b>\n"
        f"Servis: <b>{html.escape(tariff.label)}</b>\n"
        f"Key: <code>{html.escape(tariff.key)}</code>\n"
        f"Kategoriya: <b>{html.escape(category)}</b>\n\n"
        f"Free: <b>{tariff.free_cost}</b> token\n"
        f"Premium: <b>{tariff.premium_cost}</b> token\n\n"
        "Pastdagi tugmalar bilan qiymatni o'zgartiring."
    )


def _economy_overview_text() -> str:
    settings = economy_settings()
    rows = [
        "<b>Referral & reset sozlamalari</b>",
        "Referral bonuslari va free refill parametrlarini shu yerdan boshqaring.",
        "",
    ]
    for key, label in ECONOMY_SETTING_LABELS.items():
        value = int(settings.get(key, 0) or 0)
        suffix = " soat" if key == "free_reset_hours" else " token"
        if key.startswith("referral_"):
            suffix = " token"
        rows.append(f"- <b>{html.escape(label)}</b>: <b>{value}</b>{suffix}")
    rows.append("")
    rows.append("Tahrirlash uchun parametrni tanlang.")
    return "\n".join(rows)


def _economy_item_text(key: str) -> str:
    settings = economy_settings()
    label = ECONOMY_SETTING_LABELS.get(key, key.replace("_", " ").title())
    value = int(settings.get(key, 0) or 0)
    suffix = " soat" if key == "free_reset_hours" else " token"
    if key.startswith("referral_"):
        suffix = " token"
    details = {
        "referral_inviter_bonus": "Do'st taklif qilgan userga beriladi.",
        "referral_invitee_bonus": "Botga link orqali kirgan yangi userga beriladi.",
        "free_reset_tokens": "Free user balansini har intervalda shu qiymatgacha tiklaydi.",
        "free_reset_hours": "Free user refill intervali.",
    }
    return (
        "<b>Referral & reset tahriri</b>\n"
        f"Parametr: <b>{html.escape(label)}</b>\n"
        f"Key: <code>{html.escape(key)}</code>\n"
        f"Joriy qiymat: <b>{value}</b>{suffix}\n\n"
        f"{html.escape(details.get(key, ''))}\n\n"
        "Pastdagi tugmalar bilan qiymatni o'zgartiring."
    )


def _dashboard_text(snapshot: dict[str, object]) -> str:
    totals = snapshot.get("totals") if isinstance(snapshot, dict) else {}
    services = snapshot.get("services") if isinstance(snapshot, dict) else {}
    broadcasts = snapshot.get("broadcast_history") if isinstance(snapshot, dict) else []
    users = snapshot.get("users") if isinstance(snapshot, dict) else {}

    total_users = len(users) if isinstance(users, dict) else 0
    total_messages = int(totals.get("messages", 0)) if isinstance(totals, dict) else 0
    total_callbacks = int(totals.get("callbacks", 0)) if isinstance(totals, dict) else 0
    total_downloads = int(totals.get("downloads", 0)) if isinstance(totals, dict) else 0
    total_broadcasts = int(totals.get("broadcasts", 0)) if isinstance(totals, dict) else 0

    rows = [
        "<b>Admin panel</b>",
        "",
        f"Foydalanuvchilar: <b>{total_users}</b>",
        f"Xabarlar: <b>{total_messages}</b>",
        f"Callbacklar: <b>{total_callbacks}</b>",
        f"Yuklashlar: <b>{total_downloads}</b>",
        f"Broadcastlar: <b>{total_broadcasts}</b>",
    ]

    if isinstance(services, dict) and services:
        rows.append("")
        rows.append("<b>Top xizmatlar</b>")
        for key, value in sorted(
            services.items(),
            key=lambda item: int(item[1]),
            reverse=True,
        )[:8]:
            rows.append(f"- {_fmt_service_name(str(key))}: {int(value)}")

    if isinstance(broadcasts, list) and broadcasts:
        rows.append("")
        rows.append("<b>So'nggi broadcastlar</b>")
        for item in broadcasts[:5]:
            if not isinstance(item, dict):
                continue
            sent_at = str(item.get("sent_at", "")).replace("T", " ")[:16]
            sent = int(item.get("sent", 0))
            failed = int(item.get("failed", 0))
            rows.append(f"- {sent_at}: {sent} ok / {failed} fail")

    return "\n".join(rows)


def _users_text(users: Iterable[dict[str, object]]) -> str:
    rows = ["<b>So'nggi foydalanuvchilar</b>", ""]
    for item in users:
        user_id = int(item.get("user_id", 0) or 0)
        username = str(item.get("username", "") or "").strip()
        full_name = str(item.get("full_name", "") or "").strip()
        label = full_name or username or str(user_id)
        if username:
            label = f"{label} (@{username})"
        last_seen = str(item.get("last_seen", "")).replace("T", " ")[:16]
        messages = int(item.get("messages", 0) or 0)
        downloads = int(item.get("downloads", 0) or 0)
        rows.append(
            f"- <b>{html.escape(label)}</b>\n"
            f"  ID: <code>{user_id}</code> | Xabarlar: {messages} | Yuklashlar: {downloads}\n"
            f"  So'nggi faollik: {html.escape(last_seen)}"
        )
    if len(rows) == 2:
        rows.append("Hali foydalanuvchi topilmadi.")
    return "\n".join(rows)


async def _safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except TelegramBadRequest:
        await callback.message.answer(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )


async def _show_panel(
    callback: CallbackQuery,
    analytics_store: AnalyticsStore,
) -> None:
    snapshot = await analytics_store.snapshot()
    await _safe_edit(callback, _dashboard_text(snapshot), admin_keyboard())


@router.callback_query(F.data == "admin:tokens")
async def admin_tokens_panel(callback: CallbackQuery) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await callback.answer()
    await _safe_edit(callback, _token_overview_text(), _token_categories_keyboard())


@router.callback_query(F.data.startswith("admin:tokens:cat:"))
async def admin_tokens_category(callback: CallbackQuery) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    category = str(callback.data or "").split(":")[-1].strip().lower()
    if category not in set(tariff_categories()):
        await callback.answer("Kategoriya topilmadi", show_alert=True)
        return
    await callback.answer()
    await _safe_edit(
        callback,
        _token_category_text(category),
        _token_services_keyboard(category),
    )


@router.callback_query(F.data.startswith("admin:tokens:svc:"))
async def admin_tokens_service(callback: CallbackQuery) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    key = _token_service_key_from_callback(str(callback.data or ""))
    if not key:
        await callback.answer("Servis topilmadi", show_alert=True)
        return
    try:
        tariff = service_tariff(key)
    except KeyError:
        await callback.answer("Servis topilmadi", show_alert=True)
        return
    await callback.answer()
    await _safe_edit(
        callback,
        _token_service_text(tariff),
        _token_adjust_keyboard(tariff),
    )


@router.callback_query(F.data.startswith("admin:tokens:adj:"))
async def admin_tokens_adjust(callback: CallbackQuery) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parsed = _token_adjust_payload_from_callback(str(callback.data or ""))
    if parsed is None:
        await callback.answer("Format noto'g'ri", show_alert=True)
        return
    key, plan, delta = parsed
    if plan not in {"free", "premium"}:
        await callback.answer("Plan noto'g'ri", show_alert=True)
        return
    try:
        current = service_tariff(key)
    except KeyError:
        await callback.answer("Servis topilmadi", show_alert=True)
        return
    if plan == "free":
        updated = set_service_tariff_cost(key, free_cost=current.free_cost + delta)
    else:
        updated = set_service_tariff_cost(key, premium_cost=current.premium_cost + delta)
    await callback.answer("Tarif yangilandi")
    await _safe_edit(
        callback,
        _token_service_text(updated),
        _token_adjust_keyboard(updated),
    )


@router.callback_query(F.data.startswith("admin:tokens:reset:"))
async def admin_tokens_reset(callback: CallbackQuery) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    key = _token_reset_key_from_callback(str(callback.data or ""))
    if not key:
        await callback.answer("Format noto'g'ri", show_alert=True)
        return
    try:
        updated = reset_service_tariff(key)
    except KeyError:
        await callback.answer("Servis topilmadi", show_alert=True)
        return
    await callback.answer("Default tarif qaytarildi")
    await _safe_edit(
        callback,
        _token_service_text(updated),
        _token_adjust_keyboard(updated),
    )


@router.callback_query(F.data == "admin:economy")
async def admin_economy_panel(callback: CallbackQuery) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await callback.answer()
    await _safe_edit(callback, _economy_overview_text(), _economy_keyboard())


@router.callback_query(F.data.startswith("admin:economy:item:"))
async def admin_economy_item(callback: CallbackQuery) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    key = str(callback.data or "").rsplit(":", 1)[-1].strip().lower()
    if key not in ECONOMY_SETTING_LABELS:
        await callback.answer("Parametr topilmadi", show_alert=True)
        return
    await callback.answer()
    await _safe_edit(callback, _economy_item_text(key), _economy_item_keyboard(key))


@router.callback_query(F.data.startswith("admin:economy:adj:"))
async def admin_economy_adjust(callback: CallbackQuery) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = str(callback.data or "").split(":")
    if len(parts) != 5:
        await callback.answer("Format noto'g'ri", show_alert=True)
        return
    key = parts[3].strip().lower()
    if key not in ECONOMY_SETTING_LABELS:
        await callback.answer("Parametr topilmadi", show_alert=True)
        return
    try:
        delta = int(parts[4])
    except ValueError:
        await callback.answer("Qiymat noto'g'ri", show_alert=True)
        return
    current = int(economy_settings().get(key, 0) or 0)
    set_economy_setting(key, current + delta)
    await callback.answer("Sozlama yangilandi")
    await _safe_edit(callback, _economy_item_text(key), _economy_item_keyboard(key))


@router.callback_query(F.data.startswith("admin:economy:reset:"))
async def admin_economy_reset(callback: CallbackQuery) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    key = str(callback.data or "").rsplit(":", 1)[-1].strip().lower()
    if key not in ECONOMY_SETTING_LABELS:
        await callback.answer("Parametr topilmadi", show_alert=True)
        return
    reset_economy_setting(key)
    await callback.answer("Default qiymat qaytarildi")
    await _safe_edit(callback, _economy_item_text(key), _economy_item_keyboard(key))


@router.message(Command("admin"))
async def admin_entry_handler(
    message: Message,
    state: FSMContext,
    analytics_store: AnalyticsStore,
) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_admin_user_id(user_id):
        await message.answer("Admin panel siz uchun yopiq.")
        return
    await state.clear()
    snapshot = await analytics_store.snapshot()
    await message.answer(
        _dashboard_text(snapshot),
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )


@router.callback_query(F.data == "admin:panel")
async def admin_panel_callback(
    callback: CallbackQuery,
    state: FSMContext,
    analytics_store: AnalyticsStore,
) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _show_panel(callback, analytics_store)


@router.callback_query(F.data == "admin:stats")
async def admin_stats_callback(
    callback: CallbackQuery,
    analytics_store: AnalyticsStore,
) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await callback.answer()
    await _show_panel(callback, analytics_store)


@router.callback_query(F.data == "admin:users")
async def admin_users_callback(
    callback: CallbackQuery,
    analytics_store: AnalyticsStore,
) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await callback.answer()
    users = await analytics_store.recent_users()
    await _safe_edit(callback, _users_text(users), admin_keyboard())


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.set_state(AdminState.waiting_broadcast_message)
    await callback.answer()
    await _safe_edit(
        callback,
        (
            "<b>Reklama yuborish</b>\n"
            "Matn, rasm, video yoki forward qilingan xabar yuboring.\n"
            "Keyingi qadamda tasdiqlab barcha foydalanuvchilarga jo'natasiz."
        ),
        admin_broadcast_keyboard(),
    )


@router.message(AdminState.waiting_broadcast_message)
async def admin_broadcast_preview(
    message: Message,
    state: FSMContext,
) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not is_admin_user_id(user_id):
        return
    await state.update_data(
        admin_broadcast_chat_id=message.chat.id,
        admin_broadcast_message_id=message.message_id,
    )
    await message.answer(
        "<b>Preview saqlandi.</b>\nTasdiqlasangiz, reklama barcha foydalanuvchilarga yuboriladi.",
        parse_mode="HTML",
        reply_markup=admin_confirm_keyboard(),
    )


@router.callback_query(F.data == "admin:broadcast:cancel")
async def admin_broadcast_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    analytics_store: AnalyticsStore,
) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.clear()
    await callback.answer("Bekor qilindi")
    await _show_panel(callback, analytics_store)


@router.callback_query(F.data == "admin:broadcast:send")
async def admin_broadcast_send(
    callback: CallbackQuery,
    state: FSMContext,
    analytics_store: AnalyticsStore,
) -> None:
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return

    data = await state.get_data()
    source_chat_id = data.get("admin_broadcast_chat_id")
    message_id = data.get("admin_broadcast_message_id")
    if not isinstance(source_chat_id, int) or not isinstance(message_id, int):
        await callback.answer("Preview topilmadi", show_alert=True)
        return

    await callback.answer("Yuborilmoqda...")
    progress_message = await callback.message.answer(
        "<b>Broadcast boshlandi...</b>",
        parse_mode="HTML",
    )

    sent = 0
    failed = 0
    recipients = [
        user_id
        for user_id in await analytics_store.user_ids()
        if user_id != (callback.from_user.id if callback.from_user else None)
    ]

    for index, user_id in enumerate(recipients, start=1):
        try:
            await callback.bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=message_id,
            )
            sent += 1
        except TelegramRetryAfter as error:
            await asyncio.sleep(float(error.retry_after) + 1)
            try:
                await callback.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=source_chat_id,
                    message_id=message_id,
                )
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1

        if index % 25 == 0:
            await progress_message.edit_text(
                (
                    "<b>Broadcast davom etmoqda...</b>\n"
                    f"Yuborildi: <b>{sent}</b>\n"
                    f"Xato: <b>{failed}</b>"
                ),
                parse_mode="HTML",
            )
        await asyncio.sleep(0.05)

    await analytics_store.record_broadcast(sent=sent, failed=failed)
    await state.clear()
    await progress_message.edit_text(
        (
            "<b>Broadcast tugadi</b>\n"
            f"Yuborildi: <b>{sent}</b>\n"
            f"Xato: <b>{failed}</b>"
        ),
        parse_mode="HTML",
        reply_markup=admin_keyboard(),
    )
