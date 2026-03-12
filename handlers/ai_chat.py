from __future__ import annotations

import html
import re
import time

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
from services.ai_channel_logger import log_ai_exchange, remember_channel
from services.ai_gateway import (
    MODEL_ALIAS_AUTO,
    allowed_model_aliases_for_plan,
    effective_selected_plan,
    estimate_credits,
    generate_ai_reply,
    model_label,
    model_options_for_plan,
    projected_credits,
)
from services.ai_store import AIStore
from services.group_command_mode import is_group_chat
from services.token_billing import ensure_balance
from ui.premium import upgrade_prompt_keyboard

router = Router(name="ai_chat")
AI_STREAM_PREVIEW_LIMIT = 3200
AI_FINAL_TEXT_LIMIT = 3200


class AIChatState(StatesGroup):
    waiting_prompt = State()


def _user_identity(message_or_callback: Message | CallbackQuery) -> tuple[int, str, str]:
    from_user = message_or_callback.from_user
    if from_user is None:
        return 0, "", ""
    full_name = " ".join(
        part
        for part in [
            str(from_user.first_name or "").strip(),
            str(from_user.last_name or "").strip(),
        ]
        if part
    ).strip()
    return int(from_user.id), str(from_user.username or "").strip(), full_name


def _selected_model_label(user: dict[str, object]) -> str:
    return model_label(
        str(user.get("selected_model", MODEL_ALIAS_AUTO) or MODEL_ALIAS_AUTO)
    )


def _friendly_ai_error(error: Exception) -> str:
    raw = str(error or "").strip().lower()
    if "401" in raw or "user not found" in raw or "provider kaliti" in raw:
        return "🔐 AI hozir sozlanmagan. Admin API kalitini yangilashi kerak."
    if "429" in raw or "rate limit" in raw:
        return "⏳ AI limitga yetdi. Birozdan keyin qayta urinib koring."
    if "timeout" in raw or "timed out" in raw:
        return "⌛ AI javobi kechikdi. Qisqaroq sorov bilan qayta urinib koring."
    if "500" in raw or "502" in raw or "503" in raw or "504" in raw:
        return "🛠️ AI server vaqtincha javob bermayapti. Keyinroq urinib koring."
    return "⚠️ Sorov hozir bajarilmadi. Keyinroq qayta urinib koring."


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
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✨ Yangi chat", callback_data="ai:dashboard"),
                InlineKeyboardButton(text="🧠 Model", callback_data="ai:model_menu"),
            ],
            [InlineKeyboardButton(text="🧹 Kontekstni tozalash", callback_data="ai:clear")],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="services:back")],
        ]
    )


def ai_reply_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💬 Yana yozish", callback_data="ai:dashboard"),
                InlineKeyboardButton(text="🧠 Model", callback_data="ai:model_menu"),
            ],
            [InlineKeyboardButton(text="🧹 Tozalash", callback_data="ai:clear")],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="services:back")],
        ]
    )


def _model_menu_keyboard(user: dict[str, object]) -> InlineKeyboardMarkup:
    target_plan = effective_selected_plan(user)
    selected_model = str(user.get("selected_model", MODEL_ALIAS_AUTO) or MODEL_ALIAS_AUTO).strip().lower()
    rows: list[list[InlineKeyboardButton]] = []
    options = model_options_for_plan(target_plan)
    for index in range(0, len(options), 2):
        row: list[InlineKeyboardButton] = []
        for alias, label in options[index : index + 2]:
            prefix = "✅" if selected_model == alias else "🧠"
            row.append(
                InlineKeyboardButton(
                    text=f"{prefix} {label}",
                    callback_data=f"ai:model:set:{alias}",
                )
            )
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data="ai:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _dashboard_text(user: dict[str, object]) -> str:
    token_balance = int(user.get("token_balance", 0) or 0)
    total_in = int(user.get("total_prompt_tokens", 0) or 0)
    total_out = int(user.get("total_completion_tokens", 0) or 0)
    rows = [
        "<b>🤖 AI Chat</b>",
        "",
        f"🧠 Tanlangan model: <b>{html.escape(_selected_model_label(user))}</b>",
        f"💳 Balans: <b>{token_balance}</b> token",
        f"📥 Input tokenlar: <b>{total_in}</b>",
        f"📤 Output tokenlar: <b>{total_out}</b>",
    ]
    rows.append("")
    rows.append("💬 Savol yuboring. Bot kerak bolsa modelni ozi tanlaydi.")
    return "\n".join(rows)


def _legacy_plans_text() -> str:
    return (
        "<b>📚 AI tariflari bu bolimdan olib tashlangan.</b>\n"
        "⭐ Premium uchun alohida sahifadan foydalaning."
    )


def _model_menu_text(user: dict[str, object]) -> str:
    return (
        "<b>🧠 Model tanlash</b>\n\n"
        f"🧠 Tanlangan model: <b>{html.escape(_selected_model_label(user))}</b>\n\n"
        "🤖 Auto rejim botga modelni ozi tanlash imkonini beradi."
    )


def _trim_ai_text(text: str, *, limit: int) -> str:
    clean = str(text or "").strip()
    if len(clean) <= limit:
        return clean
    return f"{clean[:limit].rstrip()}..."


def _markdown_to_telegram_html(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").strip()
    if not normalized:
        return ""

    code_blocks: dict[str, str] = {}

    def capture_code_block(match: re.Match[str]) -> str:
        key = f"AICODEBLOCK{len(code_blocks)}TOKEN"
        code_blocks[key] = f"<pre>{html.escape(match.group(1).strip())}</pre>"
        return key

    normalized = re.sub(
        r"```(?:[^\n`]*)\n(.*?)```",
        capture_code_block,
        normalized,
        flags=re.S,
    )
    escaped = html.escape(normalized)

    link_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
    escaped = link_pattern.sub(
        lambda match: (
            f'<a href="{html.escape(match.group(2), quote=True)}">{match.group(1)}</a>'
        ),
        escaped,
    )
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__([^_\n]+)__", r"<b>\1</b>", escaped)
    escaped = re.sub(r"~~([^~\n]+)~~", r"<s>\1</s>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", escaped)
    escaped = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", escaped)

    rendered_lines: list[str] = []
    for raw_line in escaped.split("\n"):
        stripped = raw_line.lstrip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            rendered_lines.append(f"<b>{heading}</b>" if heading else "")
            continue
        quote_match = re.match(r"^\s*&gt;\s?(.*)$", raw_line)
        if quote_match:
            rendered_lines.append(f"<blockquote>{quote_match.group(1)}</blockquote>")
            continue
        bullet_match = re.match(r"^(\s*)[-*]\s+(.+)$", raw_line)
        if bullet_match:
            rendered_lines.append(f"{bullet_match.group(1)}- {bullet_match.group(2)}")
            continue
        rendered_lines.append(raw_line)

    rendered = "\n".join(rendered_lines)
    for key, value in code_blocks.items():
        rendered = rendered.replace(key, value)
    return rendered


def _render_stream_preview(text: str) -> str:
    preview = _trim_ai_text(text, limit=AI_STREAM_PREVIEW_LIMIT)
    if not preview:
        return "<i>AI yozmoqda...</i>"
    return f"{html.escape(preview)}\n\n<i>AI yozmoqda...</i>"


def _render_final_answer(text: str, footer: str) -> str:
    answer_text = _trim_ai_text(text, limit=AI_FINAL_TEXT_LIMIT) or "Javob bosh qaytdi."
    rendered = _markdown_to_telegram_html(answer_text)
    if not rendered:
        rendered = html.escape(answer_text)
    return f"{rendered}{footer}"


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
    await _safe_edit_or_answer(
        target,
        text=_dashboard_text(user),
        reply_markup=ai_dashboard_keyboard(),
    )


async def _show_model_menu(
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
    await _safe_edit_or_answer(
        target,
        text=_model_menu_text(user),
        reply_markup=_model_menu_keyboard(user),
    )


@router.callback_query(F.data == "services:ai")
async def ai_entry_handler(
    callback: CallbackQuery,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    await state.set_state(AIChatState.waiting_prompt)
    await callback.answer()
    user_id, username, full_name = _user_identity(callback)
    await _show_dashboard(
        callback,
        ai_store=ai_store,
        user_id=user_id,
        username=username,
        full_name=full_name,
    )


@router.callback_query(F.data == "ai:dashboard")
async def ai_dashboard_callback(
    callback: CallbackQuery,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    await state.set_state(AIChatState.waiting_prompt)
    await callback.answer()
    user_id, username, full_name = _user_identity(callback)
    await _show_dashboard(
        callback,
        ai_store=ai_store,
        user_id=user_id,
        username=username,
        full_name=full_name,
    )


@router.callback_query(F.data == "ai:plans")
async def ai_plans_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await _safe_edit_or_answer(
        callback,
        text=_legacy_plans_text(),
        reply_markup=upgrade_prompt_keyboard(),
    )


@router.callback_query(F.data == "ai:plan_menu")
async def ai_plan_menu_callback(callback: CallbackQuery) -> None:
    await callback.answer(
        "📚 AI tarif tanlovi olib tashlangan. Premium alohida sahifada.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("ai:plan:set:"))
async def ai_plan_set_callback(callback: CallbackQuery) -> None:
    await callback.answer(
        "📚 AI tarif tanlovi endi ishlatilmaydi.",
        show_alert=True,
    )


@router.callback_query(F.data == "ai:model_menu")
async def ai_model_menu_callback(
    callback: CallbackQuery,
    ai_store: AIStore,
) -> None:
    await callback.answer()
    user_id, username, full_name = _user_identity(callback)
    await _show_model_menu(
        callback,
        ai_store=ai_store,
        user_id=user_id,
        username=username,
        full_name=full_name,
    )


@router.callback_query(F.data.startswith("ai:model:set:"))
async def ai_model_set_callback(
    callback: CallbackQuery,
    ai_store: AIStore,
) -> None:
    selected_model = str(callback.data or "").rsplit(":", 1)[-1].strip().lower()
    user_id, username, full_name = _user_identity(callback)
    user = await ai_store.ensure_user(
        user_id=user_id,
        username=username,
        full_name=full_name,
    )
    target_plan = effective_selected_plan(user)
    if (
        selected_model != MODEL_ALIAS_AUTO
        and selected_model not in allowed_model_aliases_for_plan(target_plan)
    ):
        await callback.answer("Bu model hozir mavjud emas.", show_alert=True)
        return
    await ai_store.set_user_selected_model(
        user_id=user_id,
        username=username,
        full_name=full_name,
        selected_model=selected_model,
    )
    await callback.answer("✅ Model yangilandi")
    await _show_dashboard(
        callback,
        ai_store=ai_store,
        user_id=user_id,
        username=username,
        full_name=full_name,
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
    user_id, username, full_name = _user_identity(message)
    await _show_dashboard(
        message,
        ai_store=ai_store,
        user_id=user_id,
        username=username,
        full_name=full_name,
    )


@router.message(Command("ai_diag"))
async def ai_diag_command(
    message: Message,
    ai_store: AIStore,
) -> None:
    if not is_admin_user_id(message.from_user.id if message.from_user else None):
        return
    if message.from_user is None:
        return
    user_id, username, full_name = _user_identity(message)
    user = await ai_store.ensure_user(
        user_id=user_id,
        username=username,
        full_name=full_name,
    )
    try:
        decision, result = await generate_ai_reply(
            user_text="Salom",
            history=[],
            current_plan=str(user.get("current_plan", "free") or "free"),
            effective_plan=effective_selected_plan(user),
            selected_model_alias=str(
                user.get("selected_model", MODEL_ALIAS_AUTO) or MODEL_ALIAS_AUTO
            ),
        )
        await message.answer(
            (
                "<b>🩺 AI diagnostika</b>\n"
                f"Status: <b>OK</b>\n"
                f"Model: <code>{html.escape(result.model)}</code>\n"
                f"Route: <code>{html.escape(decision.route)}</code>"
            ),
            parse_mode="HTML",
        )
    except Exception as error:  # noqa: BLE001
        await message.answer(
            (
                "<b>🩺 AI diagnostika</b>\n"
                f"Status: <b>Xato</b>\n"
                f"Sabab: <code>{html.escape(str(error)[:350])}</code>"
            ),
            parse_mode="HTML",
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
    user_id, _, _ = _user_identity(callback)
    await ai_store.clear_conversation(user_id=user_id)
    await state.set_state(AIChatState.waiting_prompt)
    await callback.answer("🧹 Kontekst tozalandi")
    user_id, username, full_name = _user_identity(callback)
    await _show_dashboard(
        callback,
        ai_store=ai_store,
        user_id=user_id,
        username=username,
        full_name=full_name,
    )


@router.message(AIChatState.waiting_prompt, F.text & ~F.text.startswith("/"))
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

    user_id, username, full_name = _user_identity(message)
    user, effective_plan, wait_seconds = await ai_store.check_request_limits(
        user_id=user_id,
        username=username,
        full_name=full_name,
    )
    if wait_seconds > 0:
        token_balance = int(user.get("token_balance", 0) or 0)
        if token_balance <= 0:
            await message.answer(
                (
                    "<b>⛔ Token tugagan</b>\n"
                    f"Keyingi refillgacha taxminan <b>{wait_seconds}</b> soniya qoldi."
                ),
                parse_mode="HTML",
                reply_markup=None if is_group_chat(message) else upgrade_prompt_keyboard(),
            )
        else:
            await message.answer(
                (
                    "<b>⏳ Limit kutish rejimi</b>\n"
                    f"Keyingi AI sorov uchun <b>{wait_seconds}</b> soniya kuting."
                ),
                parse_mode="HTML",
                reply_markup=ai_dashboard_keyboard(),
            )
        return

    selected_model_alias = str(
        user.get("selected_model", MODEL_ALIAS_AUTO) or MODEL_ALIAS_AUTO
    )
    projected_cost = projected_credits(
        user_text=text,
        current_plan=str(user.get("current_plan", "free") or "free"),
        effective_plan=effective_plan,
        selected_model_alias=selected_model_alias,
    )
    charge = await ensure_balance(
        ai_store,
        message,
        "ai_chat",
        custom_cost=projected_cost,
        reply_markup=ai_dashboard_keyboard(),
    )
    if charge is None:
        return

    history = await ai_store.get_conversation(user_id=user_id)
    progress_message = await message.answer(
        "<i>AI yozmoqda...</i>",
        parse_mode="HTML",
    )
    last_stream_text = ""
    last_stream_at = 0.0

    async def on_stream_text(current_text: str) -> None:
        nonlocal last_stream_text, last_stream_at
        clean = str(current_text or "").strip()
        if not clean or clean == last_stream_text:
            return
        now = time.monotonic()
        if len(clean) - len(last_stream_text) < 120 and (now - last_stream_at) < 0.8:
            return
        try:
            await progress_message.edit_text(
                _render_stream_preview(clean),
                parse_mode="HTML",
            )
        except TelegramBadRequest as error:
            if "message is not modified" not in (error.message or "").lower():
                pass
        last_stream_text = clean
        last_stream_at = now

    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            decision, result = await generate_ai_reply(
                user_text=text,
                history=history,
                current_plan=str(user.get("current_plan", "free") or "free"),
                effective_plan=effective_plan,
                selected_model_alias=selected_model_alias,
                on_text=on_stream_text,
            )
        credits_used = estimate_credits(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            decision=decision,
        )
        updated_user = await ai_store.record_usage(
            user_id=user_id,
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
            user_id=user_id,
            user_text=text,
            assistant_text=result.text,
        )
    except Exception as error:  # noqa: BLE001
        await ai_store.record_usage(
            user_id=user_id,
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
        error_text = f"<b>{_friendly_ai_error(error)}</b>"
        try:
            await progress_message.edit_text(
                error_text,
                parse_mode="HTML",
                reply_markup=ai_dashboard_keyboard(),
            )
        except TelegramBadRequest:
            await message.answer(
                error_text,
                parse_mode="HTML",
                reply_markup=ai_dashboard_keyboard(),
            )
        return

    footer = "\n".join(
        [
            "",
            "",
            f"🧠 Model: <b>{html.escape(model_label(decision.model_alias))}</b>",
            f"📉 Kredit sarfi: <b>{credits_used}</b>",
            f"💳 Qolgan token: <b>{int(updated_user.get('token_balance', 0) or 0)}</b>",
        ]
    )
    answer_text = result.text.strip() or "Javob bosh qaytdi."
    final_text = _render_final_answer(answer_text, footer)
    try:
        await progress_message.edit_text(
            final_text,
            parse_mode="HTML",
            reply_markup=ai_reply_keyboard(),
        )
    except TelegramBadRequest:
        await message.answer(
            final_text,
            parse_mode="HTML",
            reply_markup=ai_reply_keyboard(),
        )
    try:
        await log_ai_exchange(
            message.bot,
            user_id=user_id,
            username=username,
            full_name=full_name,
            prompt_text=text,
            answer_text=answer_text,
            current_plan=str(updated_user.get("current_plan", "free") or "free"),
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
        await message.answer("Format: /ai_set_plan <user_id> <free|premium> [credits]")
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("user_id notogri.")
        return
    plan = parts[2].strip().lower()
    credits = None
    if len(parts) >= 4:
        try:
            credits = int(parts[3])
        except ValueError:
            await message.answer("credits notogri.")
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
            "<b>✅ AI plan yangilandi</b>\n"
            f"User: <code>{user_id}</code>\n"
            f"Plan: <b>{html.escape(str(updated.get('current_plan', 'free')))}</b>\n"
            f"Token: <b>{int(updated.get('token_balance', 0) or 0)}</b>"
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
        await message.answer("Parametrlar notogri.")
        return
    updated = await ai_store.set_user_credits(user_id=user_id, credits=credits)
    await message.answer(
        (
            "<b>✅ AI kredit yangilandi</b>\n"
            f"User: <code>{user_id}</code>\n"
            f"Qolgan kredit: <b>{int(updated.get('token_balance', 0) or 0)}</b>"
        ),
        parse_mode="HTML",
    )


@router.message(AIChatState.waiting_prompt)
async def ai_fallback_handler(message: Message) -> None:
    await message.answer(
        "💬 Savolni oddiy matn korinishida yuboring.",
        reply_markup=ai_dashboard_keyboard(),
    )
