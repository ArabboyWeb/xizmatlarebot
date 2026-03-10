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

from services.ai_store import AIStore
from services.analytics_store import AnalyticsStore
from services.social_client import (
    download_social_video,
    is_social_video_url,
    social_platform_name,
)
from services.saver_client import (
    DownloadedFile,
    cleanup_download,
    detect_send_kind,
    extract_first_url,
    is_youtube_url,
    download_url,
    saver_limit_bytes,
)
from services.token_billing import ensure_balance
from services.youtube_client import download_youtube

SAVE_YOUTUBE_VIDEO_QUALITIES = {"360", "720"}
SAVE_YOUTUBE_AUDIO_BITRATES = {"128"}

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
        "Direct fayl linki, YouTube, Instagram yoki TikTok link yuboring. "
        "Bot uni yuklab, shu chatga qaytaradi.\n\n"
        f"Maksimal hajm: <b>{limit_text}</b>\n"
        "Misol:\n"
        "<code>https://example.com/file.pdf</code>\n"
        "<code>https://youtu.be/dQw4w9WgXcQ</code>\n"
        "<code>https://www.instagram.com/reel/...</code>\n"
        "<code>https://www.tiktok.com/@user/video/...</code>"
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


def save_youtube_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Video 360p", callback_data="save:youtube:video:360"),
                InlineKeyboardButton(text="Video 720p", callback_data="save:youtube:video:720"),
            ],
            [
                InlineKeyboardButton(text="Audio MP3", callback_data="save:youtube:audio:128"),
            ],
            [InlineKeyboardButton(text="Bekor qilish", callback_data="save:repeat")],
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


def _public_youtube_save_error(error: Exception) -> str:
    message = str(error or "").strip()
    lowered = message.lower()
    if isinstance(error, ValueError) and message:
        return message
    if "playlist" in lowered:
        return "Playlist emas, bitta video link yuboring."
    if "audio" in lowered and "topilmadi" in lowered:
        return "Audio formati topilmadi."
    if "video" in lowered and "topilmadi" in lowered:
        return "Tanlangan video sifati topilmadi."
    if "limit" in lowered or "katta" in lowered:
        return "Fayl limitdan katta."
    return "YouTube linkini yuklab bo'lmadi. Boshqa linkni sinab ko'ring."


def _public_social_save_error(error: Exception) -> str:
    message = str(error or "").strip()
    lowered = message.lower()
    if isinstance(error, ValueError) and message:
        return message
    if "private" in lowered or "login" in lowered or "sign in" in lowered:
        return "Private yoki cheklangan video yuborildi. Public link yuboring."
    if "video topilmadi" in lowered:
        return "Videoni topib bo'lmadi. Reel yoki TikTok video link yuboring."
    if "limit" in lowered or "katta" in lowered:
        return "Fayl limitdan katta."
    return "Instagram yoki TikTok videoni yuklab bo'lmadi. Public video link yuboring."


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


async def download_and_send_youtube(
    message: Message,
    url: str,
    *,
    mode: str,
    quality: str = "360",
    audio_bitrate: str = "128",
    title: str = "Saqlash / YouTube",
    reply_markup: InlineKeyboardMarkup | None = None,
) -> DownloadedFile:
    progress_message = await message.answer(
        "<b>YouTube yuklanmoqda...</b>\nBiroz kuting.",
        parse_mode="HTML",
    )
    downloaded: DownloadedFile | None = None
    succeeded = False
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            downloaded = await download_youtube(
                url,
                mode=mode,
                quality=quality,
                audio_bitrate=audio_bitrate,
                max_bytes=saver_limit_bytes(),
            )
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
        logger.warning("Saver YouTube xatosi: %s", error)
        with contextlib.suppress(TelegramBadRequest):
            await progress_message.edit_text(
                f"<b>YouTube xatosi</b>\n{html.escape(_public_youtube_save_error(error))}",
                parse_mode="HTML",
                reply_markup=reply_markup or save_youtube_keyboard(),
            )
        raise
    finally:
        if succeeded:
            with contextlib.suppress(TelegramBadRequest):
                await progress_message.delete()
        await cleanup_download(downloaded)


async def download_and_send_social(
    message: Message,
    url: str,
    *,
    title: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> DownloadedFile:
    platform = social_platform_name(url)
    progress_message = await message.answer(
        f"<b>{html.escape(platform)} yuklanmoqda...</b>\nBiroz kuting.",
        parse_mode="HTML",
    )
    downloaded: DownloadedFile | None = None
    succeeded = False
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            downloaded = await download_social_video(
                url,
                max_bytes=saver_limit_bytes(),
            )
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
        logger.warning("Saver social xatosi: %s", error)
        with contextlib.suppress(TelegramBadRequest):
            await progress_message.edit_text(
                f"<b>Social video xatosi</b>\n{html.escape(_public_social_save_error(error))}",
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
    ai_store: AIStore,
) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    try:
        candidate_url = extract_first_url(text)
    except ValueError:
        candidate_url = ""
    if candidate_url and is_youtube_url(candidate_url):
        await message.answer(
            (
                "<b>YouTube topildi</b>\n"
                "Saver ichida yuklash uchun formatni tanlang."
            ),
            parse_mode="HTML",
            reply_markup=save_youtube_keyboard(),
        )
        await state.update_data(save_youtube_url=candidate_url)
        return
    if candidate_url and is_social_video_url(candidate_url):
        platform = social_platform_name(candidate_url)
        charge = await ensure_balance(
            ai_store,
            message,
            "save_social_video",
            reply_markup=save_keyboard(),
        )
        if charge is None:
            return
        _user, cost, user_id, username, full_name = charge
        try:
            downloaded = await download_and_send_social(
                message,
                candidate_url,
                title=f"Saqlash / {platform}",
                reply_markup=save_result_keyboard(),
            )
        except Exception:
            return
        await ai_store.charge_tokens(
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )

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
        return

    charge = await ensure_balance(
        ai_store,
        message,
        "save_direct",
        reply_markup=save_keyboard(),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    try:
        downloaded = await download_and_send_url(
            message,
            candidate_url or text,
            title="Saqlash",
            reply_markup=save_result_keyboard(),
        )
    except Exception:
        return
    await ai_store.charge_tokens(
        user_id=user_id,
        username=username,
        full_name=full_name,
        amount=cost,
    )

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


@router.callback_query(F.data.startswith("save:youtube:"))
async def save_youtube_callback(
    callback: CallbackQuery,
    state: FSMContext,
    analytics_store: AnalyticsStore,
    ai_store: AIStore,
) -> None:
    if callback.message is None:
        await callback.answer("Xabar topilmadi", show_alert=True)
        return

    data = await state.get_data()
    url = str(data.get("save_youtube_url", "")).strip()
    if not url:
        await callback.answer("YouTube link topilmadi", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    if len(parts) != 4:
        await callback.answer("Format noto'g'ri", show_alert=True)
        return

    mode = parts[2].strip().lower()
    value = parts[3].strip().lower()
    if mode == "video" and value not in SAVE_YOUTUBE_VIDEO_QUALITIES:
        await callback.answer("Video sifati noto'g'ri", show_alert=True)
        return
    if mode == "audio" and value not in SAVE_YOUTUBE_AUDIO_BITRATES:
        await callback.answer("Audio formati noto'g'ri", show_alert=True)
        return
    if mode not in {"video", "audio"}:
        await callback.answer("Format noto'g'ri", show_alert=True)
        return

    service_key = "save_youtube_video" if mode == "video" else "save_youtube_audio"
    charge = await ensure_balance(
        ai_store,
        callback,
        service_key,
        reply_markup=save_youtube_keyboard(),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    await callback.answer("Yuklanmoqda...")
    try:
        downloaded = await download_and_send_youtube(
            callback.message,
            url,
            mode=mode,
            quality=value if mode == "video" else "360",
            audio_bitrate=value if mode == "audio" else "128",
            reply_markup=save_result_keyboard(),
        )
    except Exception:
        return
    await ai_store.charge_tokens(
        user_id=user_id,
        username=username,
        full_name=full_name,
        amount=cost,
    )

    if callback.from_user is not None:
        await analytics_store.record_download(
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
            source=downloaded.source,
            size=downloaded.size,
        )
    await state.update_data(save_youtube_url="")
    await state.set_state(SaverState.waiting_url)


@router.message(SaverState.waiting_url)
async def save_fallback_handler(message: Message) -> None:
    await message.answer(
        "Link yuboring.",
        reply_markup=save_keyboard(),
    )
