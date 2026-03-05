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

from services.translate_client import language_name, translate_text

router = Router(name="translate")
logger = logging.getLogger(__name__)

PAIRS: tuple[tuple[str, str], ...] = (
    ("auto", "uz"),
    ("auto", "en"),
    ("uz", "en"),
    ("en", "uz"),
    ("uz", "ru"),
    ("ru", "uz"),
)
ENGINES = ("auto", "google", "deeplx")


class TranslateState(StatesGroup):
    waiting_text = State()


def _engine_label(engine: str, active_engine: str) -> str:
    title = {"auto": "Auto", "google": "Google", "deeplx": "DeepLX"}.get(
        engine, engine
    )
    return f"{'[' if engine == active_engine else ''}{title}{']' if engine == active_engine else ''}"


def _pair_label(source: str, target: str, active_pair: tuple[str, str]) -> str:
    base = f"{source.upper()} -> {target.upper()}"
    return f"{'[' if active_pair == (source, target) else ''}{base}{']' if active_pair == (source, target) else ''}"


def translate_keyboard(source: str, target: str, engine: str) -> InlineKeyboardMarkup:
    active_pair = (source, target)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_pair_label("auto", "uz", active_pair),
                    callback_data="translate:pair:auto:uz",
                ),
                InlineKeyboardButton(
                    text=_pair_label("auto", "en", active_pair),
                    callback_data="translate:pair:auto:en",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_pair_label("uz", "en", active_pair),
                    callback_data="translate:pair:uz:en",
                ),
                InlineKeyboardButton(
                    text=_pair_label("en", "uz", active_pair),
                    callback_data="translate:pair:en:uz",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_pair_label("uz", "ru", active_pair),
                    callback_data="translate:pair:uz:ru",
                ),
                InlineKeyboardButton(
                    text=_pair_label("ru", "uz", active_pair),
                    callback_data="translate:pair:ru:uz",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_engine_label("auto", engine),
                    callback_data="translate:engine:auto",
                ),
                InlineKeyboardButton(
                    text=_engine_label("google", engine),
                    callback_data="translate:engine:google",
                ),
                InlineKeyboardButton(
                    text=_engine_label("deeplx", engine),
                    callback_data="translate:engine:deeplx",
                ),
            ],
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


def _get_settings(data: dict[str, object]) -> tuple[str, str, str]:
    source = str(data.get("translate_source", "auto")).lower()
    target = str(data.get("translate_target", "uz")).lower()
    engine = str(data.get("translate_engine", "auto")).lower()
    if (source, target) not in PAIRS:
        source, target = "auto", "uz"
    if engine not in ENGINES:
        engine = "auto"
    return source, target, engine


def _prompt_text(source: str, target: str, engine: str) -> str:
    return (
        "<b>Tarjimon (Googletrans/DeepLX)</b>\n"
        f"Yo'nalish: <b>{html.escape(language_name(source))} -> {html.escape(language_name(target))}</b>\n"
        f"Engine: <b>{html.escape(engine.upper())}</b>\n\n"
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
    data = await state.get_data()
    source, target, engine = _get_settings(data)
    await state.set_state(TranslateState.waiting_text)
    await _safe_edit(
        callback,
        _prompt_text(source, target, engine),
        translate_keyboard(source, target, engine),
    )


@router.callback_query(F.data == "services:translate")
async def translate_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(
        translate_source="auto",
        translate_target="uz",
        translate_engine="auto",
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


@router.callback_query(F.data.startswith("translate:pair:"))
async def translate_pair_handler(callback: CallbackQuery, state: FSMContext) -> None:
    raw = callback.data or ""
    parts = raw.split(":")
    if len(parts) != 4:
        await callback.answer("Pair noto'g'ri", show_alert=True)
        return
    source, target = parts[2].lower(), parts[3].lower()
    if (source, target) not in PAIRS:
        await callback.answer("Pair topilmadi", show_alert=True)
        return

    await state.update_data(translate_source=source, translate_target=target)
    await callback.answer("Yo'nalish saqlandi")
    await _show_menu(callback, state)


@router.callback_query(F.data.startswith("translate:engine:"))
async def translate_engine_handler(callback: CallbackQuery, state: FSMContext) -> None:
    raw = callback.data or ""
    parts = raw.split(":")
    if len(parts) != 3:
        await callback.answer("Engine noto'g'ri", show_alert=True)
        return
    engine = parts[2].lower()
    if engine not in ENGINES:
        await callback.answer("Engine topilmadi", show_alert=True)
        return

    await state.update_data(translate_engine=engine)
    await callback.answer("Engine saqlandi")
    await _show_menu(callback, state)


@router.message(TranslateState.waiting_text, F.text)
async def translate_text_handler(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    source, target, engine = _get_settings(await state.get_data())
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            result = await translate_text(text, source, target, engine)
    except Exception as error:  # noqa: BLE001
        await message.answer(
            f"<b>Tarjima xatosi</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=translate_keyboard(source, target, engine),
        )
        return

    await state.set_state(TranslateState.waiting_text)
    result_text = (
        "<b>Tarjima natijasi</b>\n"
        f"Engine: <b>{html.escape(result.engine.upper())}</b>\n"
        f"Til: <b>{html.escape(language_name(result.source))} -> {html.escape(language_name(result.target))}</b>\n\n"
        f"{html.escape(result.text)}"
    )
    if result.pronunciation:
        result_text += f"\n\nPronunciation: <code>{html.escape(result.pronunciation)}</code>"

    await message.answer(
        result_text,
        parse_mode="HTML",
        reply_markup=result_keyboard(),
    )


@router.message(TranslateState.waiting_text)
async def translate_fallback(message: Message, state: FSMContext) -> None:
    source, target, engine = _get_settings(await state.get_data())
    await message.answer(
        "Tarjima qilish uchun oddiy matn yuboring.",
        reply_markup=translate_keyboard(source, target, engine),
    )
