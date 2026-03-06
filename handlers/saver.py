from __future__ import annotations

import contextlib
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
from aiogram.types.input_file import FSInputFile
from aiogram.utils.chat_action import ChatActionSender

from services.analytics_store import AnalyticsStore
from services.saver_client import (
    DownloadedFile,
    cleanup_download,
    detect_send_kind,
    is_youtube_url,
    download_url,
    saver_limit_bytes,
)

router = Router(name="saver")
logger = logging.getLogger(__name__)


class SaverState(StatesGroup):
    waiting_url = State()


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0, size))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def save_prompt_text() -> str:
    limit_text = _format_bytes(saver_limit_bytes())
    return (
        "<b>Saqlash</b>\n"
        "To'g'ridan-to'g'ri fayl linkini yuboring. Bot uni yuklab, shu chatga qaytaradi.\n\n"
        f"Maksimal hajm: <b>{limit_text}</b>\n"
        "Misol:\n"
        "<code>https://example.com/file.pdf</code>"
    )


def save_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")]
        ]
    )


def save_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yana saqlash", callback_data="save:repeat")],
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
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in (error.message or "").lower():
            logger.warning("Saver edit xatosi: %s", error)


async def send_downloaded_file(
    message: Message,
    downloaded: DownloadedFile,
    *,
    title: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    file_input = FSInputFile(downloaded.path, filename=downloaded.file_name)
    caption = (
        "<b>Fayl tayyor</b>\n"
        f"Nomi: <code>{html.escape(downloaded.file_name)}</code>\n"
        f"Hajmi: <b>{_format_bytes(downloaded.size)}</b>\n"
        f"Manba: <b>{html.escape(title)}</b>"
    )
    send_kind = detect_send_kind(downloaded.file_name, downloaded.content_type)

    try:
        if send_kind == "photo":
            await message.answer_photo(
                photo=file_input,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
        if send_kind == "video":
            await message.answer_video(
                video=file_input,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
        if send_kind == "audio":
            await message.answer_audio(
                audio=file_input,
                caption=caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
    except Exception as error:  # noqa: BLE001
        logger.warning("Native media yuborish ishlamadi, document fallback: %s", error)

    file_input = FSInputFile(downloaded.path, filename=downloaded.file_name)

    await message.answer_document(
        document=file_input,
        caption=caption,
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


def _public_save_error(error: Exception) -> str:
    message = str(error or "").strip()
    lowered = message.lower()
    if isinstance(error, ValueError) and message:
        return message
    if "limit" in lowered or "katta" in lowered:
        return "Fayl limitdan katta."
    if "bo'sh" in lowered:
        return "Fayl bo'sh qaytdi."
    return "Linkni yuklab bo'lmadi. To'g'ridan-to'g'ri fayl link yuboring."


async def download_and_send_url(
    message: Message,
    url: str,
    *,
    title: str = "Saqlash",
    reply_markup: InlineKeyboardMarkup | None = None,
) -> DownloadedFile:
    progress_message = await message.answer(
        "<b>Yuklanmoqda...</b>\nBiroz kuting.",
        parse_mode="HTML",
    )
    downloaded: DownloadedFile | None = None
    succeeded = False
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            downloaded = await download_url(url, saver_limit_bytes())
        with contextlib.suppress(TelegramBadRequest):
            await progress_message.edit_text(
                "<b>Yuborilmoqda...</b>",
                parse_mode="HTML",
            )
        async with ChatActionSender.upload_document(
            bot=message.bot,
            chat_id=message.chat.id,
        ):
            await send_downloaded_file(
                message,
                downloaded,
                title=title,
                reply_markup=reply_markup,
            )
        succeeded = True
        return downloaded
    except Exception as error:  # noqa: BLE001
        logger.warning("Saqlash xatosi: %s", error)
        with contextlib.suppress(TelegramBadRequest):
            await progress_message.edit_text(
                f"<b>Saqlash xatosi</b>\n{html.escape(_public_save_error(error))}",
                parse_mode="HTML",
                reply_markup=reply_markup or save_keyboard(),
            )
        raise
    finally:
        if succeeded:
            with contextlib.suppress(TelegramBadRequest):
                await progress_message.delete()
        await cleanup_download(downloaded)


@router.callback_query(F.data == "services:save")
async def save_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SaverState.waiting_url)
    await callback.answer()
    await _safe_edit(callback, save_prompt_text(), save_keyboard())


@router.callback_query(F.data == "save:repeat")
async def save_repeat_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(SaverState.waiting_url)
    await _safe_edit(callback, save_prompt_text(), save_keyboard())


@router.message(SaverState.waiting_url, F.text)
async def save_url_handler(
    message: Message,
    state: FSMContext,
    analytics_store: AnalyticsStore,
) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    if is_youtube_url(text):
        await message.answer(
            "YouTube linklari uchun YouTube bo'limidan foydalaning.",
            reply_markup=save_keyboard(),
        )
        return

    try:
        downloaded = await download_and_send_url(
            message,
            text,
            title="Saqlash",
            reply_markup=save_result_keyboard(),
        )
    except Exception:
        return

    if message.from_user is not None:
        await analytics_store.record_download(
            user_id=int(message.from_user.id),
            username=str(message.from_user.username or "").strip(),
            full_name=" ".join(
                part
                for part in [
                    str(message.from_user.first_name or "").strip(),
                    str(message.from_user.last_name or "").strip(),
                ]
                if part
            ).strip(),
            source=downloaded.source,
            size=downloaded.size,
        )
    await state.set_state(SaverState.waiting_url)


@router.message(SaverState.waiting_url)
async def save_fallback_handler(message: Message) -> None:
    await message.answer(
        "Link yuboring.",
        reply_markup=save_keyboard(),
    )
