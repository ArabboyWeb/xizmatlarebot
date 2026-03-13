from __future__ import annotations

import html
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from handlers.admin import admin_ids, is_admin_user_id
from services.ai_costs import premium_financial_snapshot
from services.ai_store import AIStore
from services.group_command_mode import is_group_chat
from services.token_pricing import (
    premium_ai_chat_credit_cost,
    premium_ai_image_credit_cost,
    premium_ai_search_credit_cost,
    premium_card_number,
    premium_monthly_credits,
    premium_price_uzs,
)
from ui.premium import (
    premium_admin_request_keyboard,
    premium_page_keyboard,
    premium_upload_keyboard,
)

router = Router(name="premium")


class PremiumState(StatesGroup):
    waiting_screenshot = State()


def _format_price() -> str:
    return f"{premium_price_uzs():,}"


def _contact_url(username: str, user_id: int) -> str:
    clean_username = str(username or "").strip().lstrip("@")
    if clean_username:
        return f"https://t.me/{clean_username}"
    if int(user_id) > 0:
        return f"tg://user?id={int(user_id)}"
    return ""


def _premium_page_text(
    user: dict[str, object],
    active_request: dict[str, object] | None,
    *,
    notice: str = "",
) -> str:
    current_plan = str(user.get("current_plan", "free") or "free").strip().lower()
    credit_balance = int(user.get("credit_balance", user.get("token_balance", 0)) or 0)
    refill_label = str(user.get("next_credit_reset_at", user.get("reset_date", "")) or "").replace("T", " ")[:16]
    financials = premium_financial_snapshot()
    rows = [
        "<b>Premium</b>",
        "",
        f"Narx: <b>{_format_price()} UZS</b>",
        f"Karta: <code>{premium_card_number()}</code>",
        "",
        f"Har billing siklida: <b>{premium_monthly_credits()}</b> kredit",
        f"AI Chat: <b>{premium_ai_chat_credit_cost()}</b> kredit / xabar",
        f"AI Rasm: <b>{premium_ai_image_credit_cost()}</b> kredit / rasm",
        f"AI Web Search: <b>{premium_ai_search_credit_cost()}</b> kredit / so'rov",
        f"AI byudjet cap: <b>~${financials['premium_safe_ai_budget_usd']}</b> / oy",
        "",
        f"Joriy plan: <b>{html.escape(current_plan.title())}</b>",
        f"Balans: <b>{credit_balance}</b> kredit",
    ]
    if refill_label:
        rows.append(f"Keyingi kredit reset: <b>{html.escape(refill_label)}</b>")
    if current_plan == "premium":
        rows.extend(
            [
                "",
                "<b>Premium sizda faol.</b>",
                "Kreditlar oyiga bir marta reset qilinadi, qolgan kreditlar cheksiz yig'ilmaydi.",
            ]
        )
    elif active_request is not None:
        submitted_at = str(active_request.get("submitted_at", "") or "").replace("T", " ")[:16]
        rows.extend(
            [
                "",
                "<b>So'rov pending holatda.</b>",
                f"Yuborilgan vaqt: <b>{html.escape(submitted_at)}</b>",
                "Admin tasdiqlashini kuting.",
            ]
        )
    else:
        rows.extend(
            [
                "",
                "Tolov qilgandan keyin skrinshot yuboring.",
                "Admin tekshirganidan keyin Premium faollashadi.",
            ]
        )
    if notice:
        rows.extend(["", notice])
    return "\n".join(rows)


def _upload_prompt_text() -> str:
    return (
        "<b>Premium tolov tasdigi</b>\n\n"
        f"1. <b>{_format_price()} UZS</b> ni quyidagi kartaga otkazing:\n"
        f"<code>{premium_card_number()}</code>\n\n"
        "2. Tolov qilingandan keyin shu chatga skrinshot yuboring.\n"
        "3. Sorov pending holatga tushadi va adminlar tekshiradi."
    )


def _admin_request_caption(request: dict[str, object]) -> str:
    user_id = int(request.get("user_id", 0) or 0)
    username = str(request.get("username", "") or "").strip()
    full_name = str(request.get("full_name", "") or "").strip()
    label = full_name or f"User {user_id}"
    if username:
        label = f"{label} (@{username})"
    submitted_at = str(request.get("submitted_at", "") or "").replace("T", " ")[:16]
    return (
        "<b>Premium tolov so'rovi</b>\n"
        f"So'rov: <b>#{int(request.get('request_id', 0) or 0)}</b>\n"
        f"Foydalanuvchi: <b>{html.escape(label)}</b>\n"
        f"ID: <code>{user_id}</code>\n"
        f"Narx: <b>{_format_price()} UZS</b>\n"
        f"Yuborilgan: <b>{html.escape(submitted_at)}</b>"
    )


async def _show_premium_page(
    target: Message | CallbackQuery,
    *,
    ai_store: AIStore,
    user_id: int,
    username: str,
    full_name: str,
    notice: str = "",
) -> None:
    user = await ai_store.ensure_user(
        user_id=user_id,
        username=username,
        full_name=full_name,
    )
    active_request = await ai_store.get_active_premium_request(user_id=user_id)
    text = _premium_page_text(user, active_request, notice=notice)
    reply_markup = premium_page_keyboard(
        is_active=str(user.get("current_plan", "free") or "free").strip().lower() == "premium",
        has_pending_request=active_request is not None,
    )
    if isinstance(target, CallbackQuery) and target.message is not None:
        try:
            await target.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
        except TelegramBadRequest:
            await target.message.answer(
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
    if isinstance(target, Message):
        await target.answer(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )


def _extract_screenshot_meta(message: Message) -> tuple[str, str, str] | None:
    if message.photo:
        photo = message.photo[-1]
        return photo.file_id, photo.file_unique_id, "photo"
    if message.document is None:
        return None
    mime_type = str(message.document.mime_type or "").strip().lower()
    extension = Path(str(message.document.file_name or "")).suffix.lower()
    if mime_type.startswith("image/") or extension in {".jpg", ".jpeg", ".png", ".webp"}:
        return (
            message.document.file_id,
            message.document.file_unique_id,
            "document",
        )
    return None


async def _notify_admins(
    message: Message,
    *,
    ai_store: AIStore,
    request: dict[str, object],
) -> None:
    contact_url = _contact_url(
        str(request.get("username", "") or ""),
        int(request.get("user_id", 0) or 0),
    )
    reply_markup = premium_admin_request_keyboard(
        request_id=int(request.get("request_id", 0) or 0),
        contact_url=contact_url,
    )
    caption = _admin_request_caption(request)
    for admin_id in admin_ids():
        try:
            if str(request.get("screenshot_type", "") or "") == "photo":
                sent = await message.bot.send_photo(
                    chat_id=admin_id,
                    photo=str(request.get("screenshot_file_id", "") or ""),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            else:
                sent = await message.bot.send_document(
                    chat_id=admin_id,
                    document=str(request.get("screenshot_file_id", "") or ""),
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
        except Exception:
            continue
        await ai_store.attach_premium_request_admin_message(
            request_id=int(request.get("request_id", 0) or 0),
            chat_id=admin_id,
            message_id=sent.message_id,
        )


async def _finalize_admin_message(
    callback: CallbackQuery,
    *,
    text: str,
    request_id: int,
    contact_url: str,
) -> None:
    if callback.message is None:
        return
    reply_markup = premium_admin_request_keyboard(
        request_id=request_id,
        contact_url=contact_url,
        processed=True,
    )
    try:
        if callback.message.photo or callback.message.document:
            await callback.message.edit_caption(
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        else:
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


async def _disable_admin_buttons(
    callback: CallbackQuery,
    *,
    request: dict[str, object],
) -> None:
    reply_markup = premium_admin_request_keyboard(
        request_id=int(request.get("request_id", 0) or 0),
        contact_url=_contact_url(
            str(request.get("username", "") or ""),
            int(request.get("user_id", 0) or 0),
        ),
        processed=True,
    )
    for item in list(request.get("admin_message_refs", []) or []):
        if not isinstance(item, dict):
            continue
        chat_id = item.get("chat_id")
        message_id = item.get("message_id")
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            continue
        try:
            await callback.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
        except Exception:
            continue


@router.callback_query(F.data == "premium:page")
async def premium_page_callback(
    callback: CallbackQuery,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    if is_group_chat(callback):
        await callback.answer()
        return
    await state.clear()
    await callback.answer()
    user_id = int(callback.from_user.id if callback.from_user else 0)
    username = str(callback.from_user.username or "").strip() if callback.from_user else ""
    full_name = " ".join(
        part
        for part in [
            str(getattr(callback.from_user, "first_name", "") or "").strip(),
            str(getattr(callback.from_user, "last_name", "") or "").strip(),
        ]
        if part
    ).strip()
    await _show_premium_page(
        callback,
        ai_store=ai_store,
        user_id=user_id,
        username=username,
        full_name=full_name,
    )


@router.message(Command("premium"))
async def premium_page_command(
    message: Message,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    if is_group_chat(message) or message.from_user is None:
        return
    await state.clear()
    user_id = int(message.from_user.id)
    username = str(message.from_user.username or "").strip()
    full_name = " ".join(
        part
        for part in [
            str(message.from_user.first_name or "").strip(),
            str(message.from_user.last_name or "").strip(),
        ]
        if part
    ).strip()
    await _show_premium_page(
        message,
        ai_store=ai_store,
        user_id=user_id,
        username=username,
        full_name=full_name,
    )


@router.callback_query(F.data == "premium:buy")
async def premium_buy_callback(
    callback: CallbackQuery,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    if is_group_chat(callback):
        await callback.answer()
        return
    if callback.from_user is None:
        return
    user = await ai_store.ensure_user(
        user_id=int(callback.from_user.id),
        username=str(callback.from_user.username or "").strip(),
        full_name=" ".join(
            part
            for part in [
                str(callback.from_user.first_name or "").strip(),
                str(callback.from_user.last_name or "").strip(),
            ]
            if part
        ).strip(),
    )
    if str(user.get("current_plan", "free") or "free").strip().lower() == "premium":
        await callback.answer("Premium allaqachon faol.", show_alert=True)
        await _show_premium_page(
            callback,
            ai_store=ai_store,
            user_id=int(callback.from_user.id),
            username=str(callback.from_user.username or "").strip(),
            full_name=" ".join(
                part
                for part in [
                    str(callback.from_user.first_name or "").strip(),
                    str(callback.from_user.last_name or "").strip(),
                ]
                if part
            ).strip(),
            notice="Premium sizda allaqachon yoqilgan.",
        )
        return
    active_request = await ai_store.get_active_premium_request(user_id=int(callback.from_user.id))
    if active_request is not None:
        await callback.answer("Sizda pending so'rov bor.", show_alert=True)
        await _show_premium_page(
            callback,
            ai_store=ai_store,
            user_id=int(callback.from_user.id),
            username=str(callback.from_user.username or "").strip(),
            full_name=" ".join(
                part
                for part in [
                    str(callback.from_user.first_name or "").strip(),
                    str(callback.from_user.last_name or "").strip(),
                ]
                if part
            ).strip(),
            notice="Pending so'rov allaqachon yuborilgan.",
        )
        return
    await state.set_state(PremiumState.waiting_screenshot)
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(
            _upload_prompt_text(),
            parse_mode="HTML",
            reply_markup=premium_upload_keyboard(),
        )


@router.message(PremiumState.waiting_screenshot, F.photo | F.document)
async def premium_screenshot_handler(
    message: Message,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    if is_group_chat(message) or message.from_user is None:
        return
    screenshot = _extract_screenshot_meta(message)
    if screenshot is None:
        await message.answer(
            "Tolov skrinshotini rasm yoki image document sifatida yuboring.",
            reply_markup=premium_upload_keyboard(),
        )
        return
    user_id = int(message.from_user.id)
    username = str(message.from_user.username or "").strip()
    full_name = " ".join(
        part
        for part in [
            str(message.from_user.first_name or "").strip(),
            str(message.from_user.last_name or "").strip(),
        ]
        if part
    ).strip()
    try:
        request = await ai_store.create_premium_request(
            user_id=user_id,
            username=username,
            full_name=full_name,
            screenshot_file_id=screenshot[0],
            screenshot_file_unique_id=screenshot[1],
            screenshot_type=screenshot[2],
        )
    except ValueError as error:
        await message.answer(
            html.escape(str(error)),
            parse_mode="HTML",
            reply_markup=premium_upload_keyboard(),
        )
        return
    await _notify_admins(message, ai_store=ai_store, request=request)
    await state.clear()
    await _show_premium_page(
        message,
        ai_store=ai_store,
        user_id=user_id,
        username=username,
        full_name=full_name,
        notice="Tolov skrinshoti qabul qilindi. So'rov pending holatga o'tdi.",
    )


@router.message(PremiumState.waiting_screenshot)
async def premium_waiting_fallback(message: Message) -> None:
    await message.answer(
        "Tolov skrinshotini rasm yoki image document sifatida yuboring.",
        reply_markup=premium_upload_keyboard(),
    )


@router.callback_query(F.data.startswith("premium:approve:"))
async def premium_approve_callback(
    callback: CallbackQuery,
    ai_store: AIStore,
) -> None:
    if is_group_chat(callback):
        await callback.answer("Bu amal private chatda ishlaydi.", show_alert=True)
        return
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        request_id = int(str(callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("So'rov topilmadi", show_alert=True)
        return
    result = await ai_store.review_premium_request(
        request_id=request_id,
        reviewer_id=int(callback.from_user.id if callback.from_user else 0),
        approve=True,
    )
    if not bool(result.get("ok")):
        await callback.answer("So'rov allaqachon ko'rilgan yoki topilmadi.", show_alert=True)
        return
    request = result["request"]
    user = result["user"]
    await callback.answer("Premium tasdiqlandi")
    await _disable_admin_buttons(callback, request=request)
    await _finalize_admin_message(
        callback,
        text=_admin_request_caption(request)
        + "\n\n<b>Holat:</b> <b>Tasdiqlandi</b>",
        request_id=request_id,
        contact_url=_contact_url(
            str(request.get("username", "") or ""),
            int(request.get("user_id", 0) or 0),
        ),
    )
    try:
        await callback.bot.send_message(
            chat_id=int(request.get("user_id", 0) or 0),
            text=(
                "<b>Premium faollashtirildi</b>\n"
                f"Hisobingizga <b>{premium_monthly_credits()}</b> kredit ajratildi.\n"
                "Keyingi reset: billing sikli tugaganda.\n"
                f"Joriy balans: <b>{int(user.get('credit_balance', user.get('token_balance', 0)) or 0)}</b> kredit"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("premium:reject:"))
async def premium_reject_callback(
    callback: CallbackQuery,
    ai_store: AIStore,
) -> None:
    if is_group_chat(callback):
        await callback.answer("Bu amal private chatda ishlaydi.", show_alert=True)
        return
    if not is_admin_user_id(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    try:
        request_id = int(str(callback.data or "").rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("So'rov topilmadi", show_alert=True)
        return
    result = await ai_store.review_premium_request(
        request_id=request_id,
        reviewer_id=int(callback.from_user.id if callback.from_user else 0),
        approve=False,
    )
    if not bool(result.get("ok")):
        await callback.answer("So'rov allaqachon ko'rilgan yoki topilmadi.", show_alert=True)
        return
    request = result["request"]
    await callback.answer("So'rov rad etildi")
    await _disable_admin_buttons(callback, request=request)
    await _finalize_admin_message(
        callback,
        text=_admin_request_caption(request)
        + "\n\n<b>Holat:</b> <b>Rad etildi</b>",
        request_id=request_id,
        contact_url=_contact_url(
            str(request.get("username", "") or ""),
            int(request.get("user_id", 0) or 0),
        ),
    )
    try:
        await callback.bot.send_message(
            chat_id=int(request.get("user_id", 0) or 0),
            text=(
                "<b>Premium so'rovi rad etildi</b>\n"
                "Tolov tasdigi topilmadi yoki mos kelmadi.\n"
                "Kerak bo'lsa Premium sahifasidan qayta yuboring."
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass
