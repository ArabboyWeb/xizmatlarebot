import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.chat_action import ChatActionSender

from services.ai_store import AIStore
from services.rapidapi_shazam_client import shazam_autocomplete
from services.token_billing import ensure_balance

router = Router(name="shazam")
logger = logging.getLogger(__name__)


class ShazamState(StatesGroup):
    waiting_query = State()


def shazam_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")]
        ]
    )


def shazam_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yana qidirish", callback_data="shazam:start")],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


async def _safe_edit(
    callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=reply_markup
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in (error.message or "").lower():
            logger.warning("Shazam edit xatosi: %s", error)


async def _show_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ShazamState.waiting_query)
    await _safe_edit(
        callback,
        (
            "<b>Musiqa qidirish</b>\n"
            "Qo'shiq nomini yuboring.\n"
            "Masalan: <code>kiss the</code>"
        ),
        shazam_prompt_keyboard(),
    )


def _build_result_text(term: str, hints: list[str], tracks: list[dict[str, str]]) -> str:
    lines = [f"<b>Musiqa natijalari</b>\nSo'rov: <code>{html.escape(term)}</code>"]

    if hints:
        lines.append("\n<b>Hints:</b>")
        for item in hints[:8]:
            lines.append(f"- {html.escape(item)}")

    if tracks:
        lines.append("\n<b>Top tracks:</b>")
        for row in tracks[:8]:
            title = html.escape(row.get("title", "Track"))
            subtitle = html.escape(row.get("subtitle", ""))
            if subtitle:
                lines.append(f"- <b>{title}</b> - {subtitle}")
            else:
                lines.append(f"- <b>{title}</b>")

    if not hints and not tracks:
        lines.append("\nNatija topilmadi.")
    return "\n".join(lines)


@router.callback_query(F.data == "services:shazam")
async def shazam_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _show_prompt(callback, state)


@router.callback_query(F.data == "shazam:start")
async def shazam_start_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_prompt(callback, state)


@router.message(ShazamState.waiting_query, F.text)
async def shazam_query_handler(
    message: Message,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    query = (message.text or "").strip()
    if not query or query.startswith("/"):
        return
    charge = await ensure_balance(
        ai_store,
        message,
        "shazam_search",
        reply_markup=shazam_prompt_keyboard(),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge

    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            data = await shazam_autocomplete(query, locale="en-US")
    except Exception as error:  # noqa: BLE001
        await message.answer(
            f"<b>Shazam xatosi</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=shazam_prompt_keyboard(),
        )
        return

    await state.set_state(ShazamState.waiting_query)
    await message.answer(
        _build_result_text(
            data.get("term", query),
            list(data.get("hints", [])),
            list(data.get("tracks", [])),
        ),
        parse_mode="HTML",
        reply_markup=shazam_result_keyboard(),
    )
    await ai_store.charge_tokens(
        user_id=user_id,
        username=username,
        full_name=full_name,
        amount=cost,
    )


@router.message(ShazamState.waiting_query)
async def shazam_fallback(message: Message) -> None:
    await message.answer(
        "Shazam qidiruvi uchun matn yuboring.",
        reply_markup=shazam_prompt_keyboard(),
    )
