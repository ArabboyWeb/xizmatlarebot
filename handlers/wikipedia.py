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
from services.token_billing import ensure_balance
from services.wikipedia_client import search_wikipedia_summary

router = Router(name="wikipedia")
logger = logging.getLogger(__name__)
WIKIPEDIA_MAX_SUMMARY = 3200


class WikipediaState(StatesGroup):
    waiting_query = State()


def wikipedia_keyboard(active_lang: str) -> InlineKeyboardMarkup:
    langs = ("uz", "en", "ru")
    buttons = []
    for code in langs:
        label = code.upper()
        if code == active_lang:
            label = f"[{label}]"
        buttons.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"wikipedia:lang:{code}",
            )
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            buttons,
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
            logger.warning("Wikipedia edit xatosi: %s", error)


async def _show_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lang = str(data.get("wiki_lang", "uz")).lower()
    if lang not in {"uz", "en", "ru"}:
        lang = "uz"
    await state.update_data(wiki_lang=lang)
    await state.set_state(WikipediaState.waiting_query)
    await _safe_edit(
        callback,
        (
            "<b>Wikipedia qidiruv</b>\n"
            f"Til: <b>{lang.upper()}</b>\n"
            "Mavzu nomini yuboring. Bot maqolaning qisqa tavsifini beradi."
        ),
        wikipedia_keyboard(lang),
    )


@router.callback_query(F.data == "services:wikipedia")
async def wikipedia_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(wiki_lang="uz")
    await callback.answer()
    await _show_prompt(callback, state)


@router.callback_query(F.data.startswith("wikipedia:lang:"))
async def wikipedia_lang_handler(callback: CallbackQuery, state: FSMContext) -> None:
    raw = callback.data or ""
    parts = raw.split(":")
    if len(parts) != 3:
        await callback.answer("Til noto'g'ri", show_alert=True)
        return
    lang = parts[2].lower()
    if lang not in {"uz", "en", "ru"}:
        await callback.answer("Til noto'g'ri", show_alert=True)
        return
    await state.update_data(wiki_lang=lang)
    await callback.answer(f"{lang.upper()} tanlandi")
    await _show_prompt(callback, state)


@router.message(Command("wiki"))
@router.message(Command("wikipedia"))
async def wikipedia_command_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(wiki_lang="uz")
    await state.set_state(WikipediaState.waiting_query)
    await message.answer(
        (
            "<b>Wikipedia qidiruv</b>\n"
            "Til: <b>UZ</b>\n"
            "Mavzu nomini yuboring. Bot maqolaning qisqa tavsifini beradi."
        ),
        parse_mode="HTML",
        reply_markup=wikipedia_keyboard("uz"),
    )


@router.message(WikipediaState.waiting_query, F.text & ~F.text.startswith("/"))
async def wikipedia_query_handler(
    message: Message,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    query = (message.text or "").strip()
    if not query or query.startswith("/"):
        return

    lang = str((await state.get_data()).get("wiki_lang", "uz")).lower()
    if lang not in {"uz", "en", "ru"}:
        lang = "uz"
    charge = await ensure_balance(
        ai_store,
        message,
        "wikipedia_search",
        reply_markup=wikipedia_keyboard(lang),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge

    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            data = await search_wikipedia_summary(query, preferred_lang=lang)
    except Exception as error:  # noqa: BLE001
        await message.answer(
            f"<b>Wikipedia xatosi</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=wikipedia_keyboard(lang),
        )
        return

    summary = str(data.get("summary", "")).strip()
    if len(summary) > WIKIPEDIA_MAX_SUMMARY:
        summary = summary[:WIKIPEDIA_MAX_SUMMARY].rstrip() + "..."

    text = (
        f"<b>{html.escape(str(data.get('title', 'Wikipedia')))}</b>\n"
        f"Til: <b>{html.escape(str(data.get('lang', lang)).upper())}</b>\n\n"
        f"{html.escape(summary)}\n\n"
        f"Manba: {html.escape(str(data.get('url', '')))}"
    )
    await state.set_state(WikipediaState.waiting_query)
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=wikipedia_keyboard(lang),
    )
    await ai_store.charge_tokens(
        user_id=user_id,
        username=username,
        full_name=full_name,
        amount=cost,
    )


@router.message(WikipediaState.waiting_query)
async def wikipedia_fallback(message: Message, state: FSMContext) -> None:
    lang = str((await state.get_data()).get("wiki_lang", "uz")).lower()
    if lang not in {"uz", "en", "ru"}:
        lang = "uz"
    await message.answer(
        "Wikipedia qidiruvi uchun oddiy matn yuboring.",
        reply_markup=wikipedia_keyboard(lang),
    )
