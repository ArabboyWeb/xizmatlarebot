from __future__ import annotations

import html
import os

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender

from handlers.admin import is_admin_user_id
from services.ai_channel_logger import (
    channel_link,
    log_ai_exchange,
    remember_channel,
)
from services.ai_gateway import estimate_credits, generate_ai_reply
from services.ai_store import AIStore

router = Router(name="ai_chat")


class AIChatState(StatesGroup):
    waiting_prompt = State()


def _remaining_free_requests(user: dict[str, object]) -> int:
    free_quota = max(1, int(os.getenv("AI_FREE_DAILY_REQUESTS", "20") or "20"))
    balance = int(user.get("token_balance", 0) or 0)
    free_used = int(user.get("free_requests_used", 0) or 0)
    if str(user.get("current_plan", "free")).strip().lower() == "free":
        return balance
    return max(0, int(balance <= 0) * max(0, free_quota - free_used))


async def _safe_edit_or_answer(
    target: Message | CallbackQuery,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    if isinstance(target, CallbackQuery) and target.message is not None:
        try:
            await target.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
        except TelegramBadRequest as error:
            if "message is not modified" in (error.message or "").lower():
                return
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


def ai_dashboard_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Yangi chat", callback_data="ai:dashboard"),
            InlineKeyboardButton(text="Tariflar", callback_data="ai:plans"),
        ],
        [
            InlineKeyboardButton(text="Kontekstni tozalash", callback_data="ai:clear"),
        ],
    ]
    link = channel_link()
    if link:
        rows.append([InlineKeyboardButton(text="AI arxiv kanali", url=link)])
    rows.append([InlineKeyboardButton(text="Orqaga", callback_data="services:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ai_reply_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Yana yozish", callback_data="ai:dashboard"),
            InlineKeyboardButton(text="Kontekstni tozalash", callback_data="ai:clear"),
        ],
        [InlineKeyboardButton(text="Tariflar", callback_data="ai:plans")],
    ]
    link = channel_link()
    if link:
        rows.append([InlineKeyboardButton(text="AI arxiv kanali", url=link)])
    rows.append([InlineKeyboardButton(text="Orqaga", callback_data="services:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _plan_label(value: str) -> str:
    mapping = {
        "free": "Free",
        "premium": "Premium",
        "pro": "Pro",
    }
    return mapping.get(value, value.title())


def _dashboard_text(user: dict[str, object], effective_plan: str) -> str:
    current_plan = str(user.get("current_plan", "free") or "free").strip().lower()
    token_balance = int(user.get("token_balance", 0) or 0)
    reset_date = str(user.get("reset_date", "") or "").replace("T", " ")[:16]
    free_reset_date = str(user.get("free_reset_date", "") or "").replace("T", " ")[:16]
    total_in = int(user.get("total_prompt_tokens", 0) or 0)
    total_out = int(user.get("total_completion_tokens", 0) or 0)

    rows = [
        "<b>Sun'iy Intellekt</b>",
        "",
        f"Plan: <b>{_plan_label(current_plan)}</b>",
    ]
    if effective_plan == "free" and current_plan in {"premium", "pro"} and token_balance <= 0:
        rows.append("Holat: <b>Free fallback</b>")
        rows.append(f"Bugungi free so'rovlar: <b>{_remaining_free_requests(user)}</b>")
        rows.append(f"Free reset: <b>{html.escape(free_reset_date)}</b>")
    else:
        rows.append(f"Kredit: <b>{token_balance}</b>")
        rows.append(f"Reset: <b>{html.escape(reset_date)}</b>")
    rows.append(f"Input tokenlar: <b>{total_in}</b>")
    rows.append(f"Output tokenlar: <b>{total_out}</b>")
    rows.append("")
    rows.append("Savol yuboring. Bot smart routing bilan modelni o'zi tanlaydi.")
    return "\n".join(rows)


def _plans_text() -> str:
    return (
        "<b>AI tariflar</b>\n\n"
        "<b>Free</b>\n"
        "- kuniga 20 ta so'rov\n"
        "- har so'rov orasida 5 soniya kutish\n"
        "- OpenRouter free modellar\n\n"
        "<b>Premium</b>\n"
        "- oyiga kreditli limit\n"
        "- GPT-5 Mini va Grok Fast routing\n\n"
        "<b>Pro</b>\n"
        "- oyiga yuqori kredit limiti\n"
        "- oddiy savollar arzon modelga tushadi\n"
        "- murakkab savollar rasmiy provider modeliga o'tadi"
    )


async def _show_dashboard(
    target: Message | CallbackQuery,
    *,
    ai_store: AIStore,
    user_id: int,
    username: str,
    full_name: str,
) -> None:
    user = await ai_store.ensure_user(
        user_id=user_id,
        username=username,
        full_name=full_name,
    )
    effective_plan = "free" if (
        str(user.get("current_plan", "free")).strip().lower() in {"premium", "pro"}
        and int(user.get("token_balance", 0) or 0) <= 0
    ) else str(user.get("current_plan", "free")).strip().lower()
    text = _dashboard_text(user, effective_plan)
    await _safe_edit_or_answer(
        target,
        text=text,
        reply_markup=ai_dashboard_keyboard(),
    )


@router.callback_query(F.data == "services:ai")
async def ai_entry_handler(
    callback: CallbackQuery,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    await state.set_state(AIChatState.waiting_prompt)
    await callback.answer()
    await _show_dashboard(
        callback,
        ai_store=ai_store,
        user_id=callback.from_user.id,
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


@router.callback_query(F.data == "ai:dashboard")
async def ai_dashboard_callback(
    callback: CallbackQuery,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    await state.set_state(AIChatState.waiting_prompt)
    await callback.answer()
    await _show_dashboard(
        callback,
        ai_store=ai_store,
        user_id=callback.from_user.id,
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


@router.callback_query(F.data == "ai:plans")
async def ai_plans_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await _safe_edit_or_answer(
        callback,
        text=_plans_text(),
        reply_markup=ai_dashboard_keyboard(),
    )


@router.message(Command("ai"))
async def ai_command_handler(
    message: Message,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    if message.from_user is None:
        return
    await state.set_state(AIChatState.waiting_prompt)
    await _show_dashboard(
        message,
        ai_store=ai_store,
        user_id=message.from_user.id,
        username=str(message.from_user.username or "").strip(),
        full_name=" ".join(
            part
            for part in [
                str(message.from_user.first_name or "").strip(),
                str(message.from_user.last_name or "").strip(),
            ]
            if part
        ).strip(),
    )


@router.my_chat_member()
async def ai_log_channel_member_handler(event: ChatMemberUpdated) -> None:
    if getattr(event.chat, "type", None) != "channel":
        return
    if str(getattr(event.new_chat_member, "status", "") or "") in {
        "administrator",
        "member",
    }:
        remember_channel(event.chat)


@router.channel_post()
async def ai_log_channel_post_handler(message: Message) -> None:
    if getattr(message.chat, "type", None) == "channel":
        remember_channel(message.chat)


@router.callback_query(F.data == "ai:clear")
async def ai_clear_callback(
    callback: CallbackQuery,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    await ai_store.clear_conversation(user_id=callback.from_user.id)
    await state.set_state(AIChatState.waiting_prompt)
    await callback.answer("Kontekst tozalandi")
    await _show_dashboard(
        callback,
        ai_store=ai_store,
        user_id=callback.from_user.id,
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


@router.message(AIChatState.waiting_prompt, F.text)
async def ai_prompt_handler(
    message: Message,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    if message.from_user is None:
        return

    username = str(message.from_user.username or "").strip()
    full_name = " ".join(
        part
        for part in [
            str(message.from_user.first_name or "").strip(),
            str(message.from_user.last_name or "").strip(),
        ]
        if part
    ).strip()
    user, effective_plan, wait_seconds = await ai_store.check_request_limits(
        user_id=int(message.from_user.id),
        username=username,
        full_name=full_name,
    )
    if wait_seconds > 0:
        await message.answer(
            (
                "<b>Limit kutish rejimi</b>\n"
                f"Keyingi AI so'rov uchun <b>{wait_seconds}</b> soniya kuting."
            ),
            parse_mode="HTML",
            reply_markup=ai_dashboard_keyboard(),
        )
        return

    history = await ai_store.get_conversation(user_id=int(message.from_user.id))
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            decision, result = await generate_ai_reply(
                user_text=text,
                history=history,
                current_plan=str(user.get("current_plan", "free")),
                effective_plan=effective_plan,
            )
        credits_used = estimate_credits(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            decision=decision,
        )
        updated_user = await ai_store.record_usage(
            user_id=int(message.from_user.id),
            username=username,
            full_name=full_name,
            effective_plan=effective_plan,
            provider=result.provider,
            model=result.model,
            route=result.route,
            credits_used=credits_used,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
            ok=True,
        )
        await ai_store.append_conversation_turn(
            user_id=int(message.from_user.id),
            user_text=text,
            assistant_text=result.text,
        )
    except Exception as error:  # noqa: BLE001
        await ai_store.record_usage(
            user_id=int(message.from_user.id),
            username=username,
            full_name=full_name,
            effective_plan=effective_plan,
            provider="error",
            model="",
            route="failed",
            credits_used=0,
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
            ok=False,
            error_text=str(error),
        )
        await message.answer(
            (
                "<b>AI xatosi</b>\n"
                "So'rov hozir bajarilmadi. Keyinroq qayta urinib ko'ring."
            ),
            parse_mode="HTML",
            reply_markup=ai_dashboard_keyboard(),
        )
        return

    footer_rows = [
        "",
        "",
        f"<b>Model:</b> <code>{html.escape(result.model)}</code>",
        f"<b>Kredit sarfi:</b> <b>{credits_used}</b>",
    ]
    current_plan = str(updated_user.get("current_plan", "free") or "free").strip().lower()
    if effective_plan == "free" and current_plan in {"premium", "pro"}:
        footer_rows.append("<b>Paid kredit:</b> <b>0</b>")
        footer_rows.append(
            f"<b>Free fallback qolgan:</b> <b>{_remaining_free_requests(updated_user)}</b>"
        )
    elif current_plan == "free":
        footer_rows.append(
            f"<b>Bugungi qolgan so'rov:</b> <b>{_remaining_free_requests(updated_user)}</b>"
        )
    else:
        footer_rows.append(
            f"<b>Qolgan kredit:</b> <b>{int(updated_user.get('token_balance', 0) or 0)}</b>"
        )
    footer = "\n".join(footer_rows)
    answer_text = result.text.strip() or "Javob bo'sh qaytdi."
    safe_answer = html.escape(answer_text)
    if len(safe_answer) > 3500:
        safe_answer = f"{safe_answer[:3500]}..."
    await message.answer(
        f"{safe_answer}{footer}",
        parse_mode="HTML",
        reply_markup=ai_reply_keyboard(),
    )
    try:
        await log_ai_exchange(
            message.bot,
            user_id=int(message.from_user.id),
            username=username,
            full_name=full_name,
            prompt_text=text,
            answer_text=answer_text,
            current_plan=current_plan,
            effective_plan=effective_plan,
            model=result.model,
            credits_used=credits_used,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
    except Exception:
        pass
    await state.set_state(AIChatState.waiting_prompt)


@router.message(Command("ai_set_plan"))
async def ai_set_plan_command(
    message: Message,
    ai_store: AIStore,
) -> None:
    if not is_admin_user_id(message.from_user.id if message.from_user else None):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Format: /ai_set_plan <user_id> <free|premium|pro> [credits]")
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("user_id noto'g'ri.")
        return
    plan = parts[2].strip().lower()
    credits = None
    if len(parts) >= 4:
        try:
            credits = int(parts[3])
        except ValueError:
            await message.answer("credits noto'g'ri.")
            return
    updated = await ai_store.set_user_plan(
        user_id=user_id,
        username="",
        full_name="",
        plan=plan,
        credits=credits,
    )
    await message.answer(
        (
            "<b>AI plan yangilandi</b>\n"
            f"User: <code>{user_id}</code>\n"
            f"Plan: <b>{html.escape(str(updated.get('current_plan', 'free')))}</b>\n"
            f"Kredit: <b>{int(updated.get('token_balance', 0) or 0)}</b>"
        ),
        parse_mode="HTML",
    )


@router.message(Command("ai_set_credits"))
async def ai_set_credits_command(
    message: Message,
    ai_store: AIStore,
) -> None:
    if not is_admin_user_id(message.from_user.id if message.from_user else None):
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Format: /ai_set_credits <user_id> <credits>")
        return
    try:
        user_id = int(parts[1])
        credits = int(parts[2])
    except ValueError:
        await message.answer("Parametrlar noto'g'ri.")
        return
    updated = await ai_store.set_user_credits(user_id=user_id, credits=credits)
    await message.answer(
        (
            "<b>AI kredit yangilandi</b>\n"
            f"User: <code>{user_id}</code>\n"
            f"Qolgan kredit: <b>{int(updated.get('token_balance', 0) or 0)}</b>"
        ),
        parse_mode="HTML",
    )


@router.message(AIChatState.waiting_prompt)
async def ai_fallback_handler(message: Message) -> None:
    await message.answer(
        "Savolni oddiy matn ko'rinishida yuboring.",
        reply_markup=ai_dashboard_keyboard(),
    )
