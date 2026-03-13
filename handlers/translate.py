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
from services.rapidapi_translate_client import language_name, translate_text

router = Router(name="translate")
logger = logging.getLogger(__name__)

LANG_CODES = ("uz", "en", "ru", "zh")
DEFAULT_SOURCE = "en"
DEFAULT_TARGET = "uz"


class TranslateState(StatesGroup):
    waiting_text = State()


def _lang_label(code: str, selected: str) -> str:
    label = code.upper()
    if code == "zh":
        label = "ZH"
    return f"[{label}]" if code == selected else label


def _source_keyboard(selected_source: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text=f"S:{_lang_label(code, selected_source)}",
            callback_data=f"translate:source:{code}",
        )
        for code in LANG_CODES
    ]


def _target_keyboard(selected_target: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text=f"T:{_lang_label(code, selected_target)}",
            callback_data=f"translate:target:{code}",
        )
        for code in LANG_CODES
    ]


def translate_keyboard(source: str, target: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            _source_keyboard(source),
            _target_keyboard(target),
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


def result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yana tarjima", callback_data="translate:repeat")],
            [InlineKeyboardButton(text="Sozlamalar", callback_data="translate:menu")],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


def _settings(data: dict[str, object]) -> tuple[str, str]:
    source = str(data.get("translate_source", DEFAULT_SOURCE)).lower()
    target = str(data.get("translate_target", DEFAULT_TARGET)).lower()
    if source not in LANG_CODES:
        source = DEFAULT_SOURCE
    if target not in LANG_CODES:
        target = DEFAULT_TARGET
    return source, target


def _prompt_text(source: str, target: str) -> str:
    return (
        "<b>Tarjimon</b>\n"
        f"Yo'nalish: <b>{html.escape(language_name(source))} -> {html.escape(language_name(target))}</b>\n"
        "Tillar: UZ, EN, RU, ZH\n\n"
        "Tarjima qilish uchun matn yuboring."
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
            logger.warning("Translate edit xatosi: %s", error)


async def _show_menu(callback: CallbackQuery, state: FSMContext) -> None:
    source, target = _settings(await state.get_data())
    await state.set_state(TranslateState.waiting_text)
    await _safe_edit(
        callback,
        _prompt_text(source, target),
        translate_keyboard(source, target),
    )


@router.callback_query(F.data == "services:translate")
async def translate_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(
        translate_source=DEFAULT_SOURCE,
        translate_target=DEFAULT_TARGET,
    )
    await callback.answer()
    await _show_menu(callback, state)


@router.callback_query(F.data == "translate:menu")
async def translate_menu_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_menu(callback, state)


@router.callback_query(F.data == "translate:repeat")
async def translate_repeat_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_menu(callback, state)


@router.message(Command("translate"))
async def translate_command_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(
        translate_source=DEFAULT_SOURCE,
        translate_target=DEFAULT_TARGET,
    )
    await state.set_state(TranslateState.waiting_text)
    await message.answer(
        _prompt_text(DEFAULT_SOURCE, DEFAULT_TARGET),
        parse_mode="HTML",
        reply_markup=translate_keyboard(DEFAULT_SOURCE, DEFAULT_TARGET),
    )


@router.callback_query(F.data.startswith("translate:source:"))
async def translate_source_handler(callback: CallbackQuery, state: FSMContext) -> None:
    raw = callback.data or ""
    parts = raw.split(":")
    if len(parts) != 3:
        await callback.answer("Source noto'g'ri", show_alert=True)
        return
    source = parts[2].lower()
    if source not in LANG_CODES:
        await callback.answer("Source til topilmadi", show_alert=True)
        return
    await state.update_data(translate_source=source)
    await callback.answer("Source saqlandi")
    await _show_menu(callback, state)


@router.callback_query(F.data.startswith("translate:target:"))
async def translate_target_handler(callback: CallbackQuery, state: FSMContext) -> None:
    raw = callback.data or ""
    parts = raw.split(":")
    if len(parts) != 3:
        await callback.answer("Target noto'g'ri", show_alert=True)
        return
    target = parts[2].lower()
    if target not in LANG_CODES:
        await callback.answer("Target til topilmadi", show_alert=True)
        return
    await state.update_data(translate_target=target)
    await callback.answer("Target saqlandi")
    await _show_menu(callback, state)


@router.message(TranslateState.waiting_text, F.text & ~F.text.startswith("/"))
async def translate_text_handler(
    message: Message,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    source, target = _settings(await state.get_data())
    charge = await ensure_balance(
        ai_store,
        message,
        "translate_text",
        reply_markup=translate_keyboard(source, target),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    progress_message = await send_wait_message(
        message,
        text="<b>Iltimos kuting...</b>\nTarjima qilinmoqda.",
    )
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            result = await translate_text(text, source, target)
    except Exception as error:  # noqa: BLE001
        await clear_wait_message(progress_message)
        await message.answer(
            f"<b>Tarjima xatosi</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=translate_keyboard(source, target),
        )
        return
    await clear_wait_message(progress_message)

    await state.set_state(TranslateState.waiting_text)
    await message.answer(
        (
            "<b>Tarjima natijasi</b>\n"
            f"Til: <b>{html.escape(language_name(result.source))} -> "
            f"{html.escape(language_name(result.target))}</b>\n\n"
            f"{html.escape(result.text)}"
        ),
        parse_mode="HTML",
        reply_markup=result_keyboard(),
    )
    await ai_store.charge_tokens(
        user_id=user_id,
        username=username,
        full_name=full_name,
        amount=cost,
    )


@router.message(TranslateState.waiting_text)
async def translate_fallback(message: Message, state: FSMContext) -> None:
    source, target = _settings(await state.get_data())
    await message.answer(
        "Tarjima qilish uchun oddiy matn yuboring.",
        reply_markup=translate_keyboard(source, target),
    )
