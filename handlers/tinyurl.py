import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
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
from services.request_feedback import clear_wait_message, send_wait_message
from services.token_billing import ensure_balance
from services.tinyurl_client import shorten_url

router = Router(name="tinyurl")
logger = logging.getLogger(__name__)


class TinyUrlState(StatesGroup):
    waiting_url = State()


def tinyurl_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")]
        ]
    )


def tinyurl_result_keyboard(short_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Open TinyURL", url=short_url)],
            [InlineKeyboardButton(text="Yana qisqartirish", callback_data="tinyurl:start")],
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
            logger.warning("TinyURL edit xatosi: %s", error)


async def _show_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TinyUrlState.waiting_url)
    await _safe_edit(
        callback,
        (
            "<b>TinyURL (Free)</b>\n"
            "Qisqartirish uchun to'liq <code>http/https</code> URL yuboring.\n\n"
            "Misol:\n<code>https://example.com/very/long/link</code>"
        ),
        tinyurl_prompt_keyboard(),
    )


@router.callback_query(F.data == "services:tinyurl")
async def tinyurl_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _show_prompt(callback, state)


@router.callback_query(F.data == "tinyurl:start")
async def tinyurl_start_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_prompt(callback, state)


@router.message(Command("tinyurl"))
@router.message(Command("link"))
async def tinyurl_command_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(TinyUrlState.waiting_url)
    await message.answer(
        (
            "<b>TinyURL (Free)</b>\n"
            "Qisqartirish uchun to'liq <code>http/https</code> URL yuboring.\n\n"
            "Misol:\n<code>https://example.com/very/long/link</code>"
        ),
        parse_mode="HTML",
        reply_markup=tinyurl_prompt_keyboard(),
    )


@router.message(TinyUrlState.waiting_url, F.text & ~F.text.startswith("/"))
async def tinyurl_message_handler(
    message: Message,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    url = (message.text or "").strip()
    if not url or url.startswith("/"):
        return
    charge = await ensure_balance(
        ai_store,
        message,
        "tinyurl_create",
        reply_markup=tinyurl_prompt_keyboard(),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    progress_message = await send_wait_message(
        message,
        text="<b>Iltimos kuting...</b>\nLink qisqartirilmoqda.",
    )

    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            short, mode = await shorten_url(url)
    except Exception as error:  # noqa: BLE001
        await clear_wait_message(progress_message)
        await message.answer(
            f"<b>TinyURL xatosi</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=tinyurl_prompt_keyboard(),
        )
        return
    await clear_wait_message(progress_message)

    await state.set_state(TinyUrlState.waiting_url)
    await message.answer(
        (
            "<b>URL qisqartirildi</b>\n"
            f"Asl link: <code>{html.escape(url)}</code>\n"
            f"Qisqa link: <code>{html.escape(short)}</code>\n"
            f"Rejim: <b>{html.escape(mode)}</b>"
        ),
        parse_mode="HTML",
        reply_markup=tinyurl_result_keyboard(short),
    )
    await ai_store.charge_tokens(
        user_id=user_id,
        username=username,
        full_name=full_name,
        amount=cost,
    )


@router.message(TinyUrlState.waiting_url)
async def tinyurl_fallback(message: Message) -> None:
    await message.answer(
        "Faqat to'liq http/https link yuboring.",
        reply_markup=tinyurl_prompt_keyboard(),
    )
