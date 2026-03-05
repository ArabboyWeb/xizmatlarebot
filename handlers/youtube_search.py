import html
import logging
import os

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

from services.youtube_rapid_client import search_channel_videos

router = Router(name="youtube_search")
logger = logging.getLogger(__name__)


class YoutubeSearchState(StatesGroup):
    waiting_channel_id = State()
    waiting_query = State()


def _default_channel_id() -> str:
    return os.getenv("YOUTUBE_CHANNEL_ID", "").strip()


def youtube_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Channel ID sozlash", callback_data="youtube:set_channel")],
            [InlineKeyboardButton(text="Qidirish", callback_data="youtube:search")],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


def youtube_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Orqaga", callback_data="services:youtube")]
        ]
    )


def youtube_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yana qidirish", callback_data="youtube:search")],
            [InlineKeyboardButton(text="Menyu", callback_data="services:youtube")],
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
            logger.warning("YouTube search edit xatosi: %s", error)


async def _current_channel_id(state: FSMContext) -> str:
    data = await state.get_data()
    channel_id = str(data.get("youtube_channel_id", "")).strip()
    if channel_id:
        return channel_id
    default = _default_channel_id()
    if default:
        await state.update_data(youtube_channel_id=default)
        return default
    return ""


@router.callback_query(F.data == "services:youtube")
async def youtube_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    current = await _current_channel_id(state)
    await callback.answer()
    await _safe_edit(
        callback,
        (
            "<b>YouTube Channel Search (RapidAPI)</b>\n"
            "Bu servis channel ichida video qidiradi.\n\n"
            f"Joriy channel ID: <code>{html.escape(current or 'sozlanmagan')}</code>"
        ),
        youtube_menu_keyboard(),
    )


@router.callback_query(F.data == "youtube:set_channel")
async def youtube_set_channel_handler(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await state.set_state(YoutubeSearchState.waiting_channel_id)
    await callback.answer()
    await _safe_edit(
        callback,
        (
            "<b>Channel ID yuboring</b>\n"
            "Masalan: <code>UChPvQ8hfrSW1EAbtBWjis0g</code>"
        ),
        youtube_back_keyboard(),
    )


@router.callback_query(F.data == "youtube:search")
async def youtube_search_callback(callback: CallbackQuery, state: FSMContext) -> None:
    channel_id = await _current_channel_id(state)
    if not channel_id:
        await callback.answer("Avval channel ID sozlang.", show_alert=True)
        await _safe_edit(
            callback,
            "<b>Avval channel ID ni kiriting.</b>",
            youtube_menu_keyboard(),
        )
        return

    await state.set_state(YoutubeSearchState.waiting_query)
    await callback.answer()
    await _safe_edit(
        callback,
        (
            "<b>YouTube qidiruv so'rovi yuboring</b>\n"
            f"Channel: <code>{html.escape(channel_id)}</code>\n"
            "Masalan: <code>animal</code>"
        ),
        youtube_back_keyboard(),
    )


@router.message(YoutubeSearchState.waiting_channel_id, F.text)
async def youtube_channel_message(message: Message, state: FSMContext) -> None:
    channel_id = (message.text or "").strip()
    if not channel_id or channel_id.startswith("/"):
        return
    if len(channel_id) < 8:
        await message.answer(
            "Channel ID noto'g'ri ko'rinmoqda. Qayta yuboring.",
            reply_markup=youtube_back_keyboard(),
        )
        return

    await state.update_data(youtube_channel_id=channel_id)
    await state.set_state(YoutubeSearchState.waiting_query)
    await message.answer(
        (
            "<b>Channel ID saqlandi.</b>\n"
            f"Channel: <code>{html.escape(channel_id)}</code>\n"
            "Endi qidiruv so'rovini yuboring."
        ),
        parse_mode="HTML",
        reply_markup=youtube_back_keyboard(),
    )


def _build_youtube_text(
    channel_id: str, query: str, videos: list[dict[str, str]], next_token: str
) -> str:
    rows = [
        "<b>YouTube qidiruv natijalari</b>",
        f"Channel: <code>{html.escape(channel_id)}</code>",
        f"So'rov: <code>{html.escape(query)}</code>",
        "",
    ]
    if not videos:
        rows.append("Natija topilmadi.")
    else:
        for idx, video in enumerate(videos[:8], start=1):
            title = html.escape(video.get("title", "Video"))
            url = html.escape(video.get("url", ""))
            duration = html.escape(video.get("duration", ""))
            published = html.escape(video.get("published", ""))
            rows.append(f"{idx}. <b>{title}</b>")
            if duration:
                rows.append(f"   Davomiyligi: {duration}")
            if published:
                rows.append(f"   Vaqti: {published}")
            if url:
                rows.append(f"   {url}")
            rows.append("")
    if next_token:
        rows.append("Qo'shimcha natijalar bor (next token mavjud).")
    return "\n".join(rows).strip()


@router.message(YoutubeSearchState.waiting_query, F.text)
async def youtube_query_message(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if not query or query.startswith("/"):
        return

    channel_id = await _current_channel_id(state)
    if not channel_id:
        await message.answer(
            "Avval channel ID sozlang.",
            reply_markup=youtube_menu_keyboard(),
        )
        return

    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            result = await search_channel_videos(channel_id, query)
    except Exception as error:  # noqa: BLE001
        await message.answer(
            f"<b>YouTube search xatosi</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=youtube_result_keyboard(),
        )
        return

    await state.set_state(YoutubeSearchState.waiting_query)
    await message.answer(
        _build_youtube_text(
            result.get("channel_id", channel_id),
            result.get("query", query),
            list(result.get("videos", [])),
            str(result.get("next", "")),
        ),
        parse_mode="HTML",
        reply_markup=youtube_result_keyboard(),
    )


@router.message(YoutubeSearchState.waiting_channel_id)
async def youtube_channel_fallback(message: Message) -> None:
    await message.answer(
        "Channel ID yuboring.",
        reply_markup=youtube_back_keyboard(),
    )


@router.message(YoutubeSearchState.waiting_query)
async def youtube_query_fallback(message: Message) -> None:
    await message.answer(
        "Qidiruv so'rovini yuboring.",
        reply_markup=youtube_back_keyboard(),
    )
