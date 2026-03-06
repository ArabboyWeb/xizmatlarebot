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

router = Router(name="admin")


class AdminState(StatesGroup):
    waiting_broadcast_message = State()


def _admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_USER_IDS", "").strip()
    if not raw:
        return set()
    values: set[int] = set()
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError:
            continue
    return values


def _is_admin(user_id: int | None) -> bool:
    return isinstance(user_id, int) and user_id in _admin_ids()


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Statistika", callback_data="admin:stats"),
                InlineKeyboardButton(text="Foydalanuvchilar", callback_data="admin:users"),
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


def _fmt_service_name(key: str) -> str:
    mapping = {
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
        "wikipedia": "Maqola qidirish",
        "rembg": "Fonni olib tashlash",
        "pollinations": "Rasm yaratish",
        "download:direct": "Direct yuklash",
        "download:youtube": "YouTube yuklash",
    }
    return mapping.get(key, key.replace("_", " ").title())


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


@router.message(Command("admin"))
async def admin_entry_handler(
    message: Message,
    state: FSMContext,
    analytics_store: AnalyticsStore,
) -> None:
    user_id = message.from_user.id if message.from_user else None
    if not _is_admin(user_id):
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
    if not _is_admin(callback.from_user.id if callback.from_user else None):
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
    if not _is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await callback.answer()
    await _show_panel(callback, analytics_store)


@router.callback_query(F.data == "admin:users")
async def admin_users_callback(
    callback: CallbackQuery,
    analytics_store: AnalyticsStore,
) -> None:
    if not _is_admin(callback.from_user.id if callback.from_user else None):
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
    if not _is_admin(callback.from_user.id if callback.from_user else None):
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
    if not _is_admin(user_id):
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
    if not _is_admin(callback.from_user.id if callback.from_user else None):
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
    if not _is_admin(callback.from_user.id if callback.from_user else None):
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
