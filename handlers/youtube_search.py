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
from aiogram.utils.chat_action import ChatActionSender

from handlers.saver import send_downloaded_file
from services.ai_store import AIStore
from services.analytics_store import AnalyticsStore
from services.saver_client import (
    DownloadedFile,
    cleanup_download,
    extract_first_url,
    is_youtube_url,
    saver_limit_bytes,
)
from services.social_client import (
    download_social_video,
    is_social_video_url,
    social_platform_name,
)
from services.token_billing import ensure_balance, finalize_charge
from services.youtube_client import AUDIO_BITRATES, VIDEO_QUALITIES, download_youtube, search_youtube

router = Router(name="youtube_search")
logger = logging.getLogger(__name__)


class YoutubeState(StatesGroup):
    waiting_input = State()


def _video_mode_label(current: str, value: str, title: str) -> str:
    return f"[{title}]" if current == value else title


def _quality_label(current: str, value: str, title: str) -> str:
    return f"[{title}]" if current == value else title


def _settings(data: dict[str, object]) -> tuple[str, str, str]:
    mode = str(data.get("youtube_mode", "video")).lower()
    quality = str(data.get("youtube_quality", "best")).lower()
    audio_bitrate = str(data.get("youtube_audio_bitrate", "192"))
    if mode not in {"video", "audio"}:
        mode = "video"
    if quality not in VIDEO_QUALITIES:
        quality = "best"
    if audio_bitrate not in AUDIO_BITRATES:
        audio_bitrate = "192"
    return mode, quality, audio_bitrate


def youtube_keyboard(
    mode: str,
    quality: str,
    audio_bitrate: str,
    *,
    has_results: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=_video_mode_label(mode, "video", "Video"),
                callback_data="youtube:mode:video",
            ),
            InlineKeyboardButton(
                text=_video_mode_label(mode, "audio", "Audio"),
                callback_data="youtube:mode:audio",
            ),
        ]
    ]
    if mode == "video":
        rows.append(
            [
                InlineKeyboardButton(
                    text=_quality_label(quality, "best", "Auto"),
                    callback_data="youtube:quality:best",
                ),
                InlineKeyboardButton(
                    text=_quality_label(quality, "1080", "1080p"),
                    callback_data="youtube:quality:1080",
                ),
                InlineKeyboardButton(
                    text=_quality_label(quality, "720", "720p"),
                    callback_data="youtube:quality:720",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=_quality_label(quality, "480", "480p"),
                    callback_data="youtube:quality:480",
                ),
                InlineKeyboardButton(
                    text=_quality_label(quality, "360", "360p"),
                    callback_data="youtube:quality:360",
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_quality_label(audio_bitrate, "128", "128k"),
                    callback_data="youtube:bitrate:128",
                ),
                InlineKeyboardButton(
                    text=_quality_label(audio_bitrate, "192", "192k"),
                    callback_data="youtube:bitrate:192",
                ),
                InlineKeyboardButton(
                    text=_quality_label(audio_bitrate, "256", "256k"),
                    callback_data="youtube:bitrate:256",
                ),
            ]
        )
    if has_results:
        rows.append(
            [InlineKeyboardButton(text="Natijalarni tozalash", callback_data="youtube:clear")]
        )
    rows.append([InlineKeyboardButton(text="Orqaga", callback_data="services:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def youtube_results_keyboard(
    videos: list[dict[str, str]],
    *,
    mode: str,
    quality: str,
    audio_bitrate: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for idx, _video in enumerate(videos[:6], start=1):
        button_text = f"{idx}. {'Audio' if mode == 'audio' else 'Video'}"
        current_row.append(
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"youtube:download:{idx - 1}",
            )
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.extend(
        youtube_keyboard(
            mode,
            quality,
            audio_bitrate,
            has_results=True,
        ).inline_keyboard
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _prompt_text(mode: str, quality: str, audio_bitrate: str) -> str:
    if mode == "audio":
        settings_text = f"Rejim: <b>Audio</b>\nBitrate: <b>{audio_bitrate}k</b>"
    else:
        settings_text = f"Rejim: <b>Video</b>\nSifat: <b>{quality.upper() if quality != 'best' else 'AUTO'}</b>"
    return (
        "<b>YouTube / Instagram / TikTok Saver</b>\n"
        "YouTube qidiruv matni yoki YouTube, Instagram, TikTok link yuboring.\n"
        "YouTube uchun qidiruv ishlaydi, Instagram/TikTok uchun direct link bilan yuklaydi.\n"
        "Free foydalanuvchi direct link bilan har refill siklida 1 marta tekin yuklay oladi.\n\n"
        f"{settings_text}\n\n"
        "Misollar:\n"
        "<code>lofi hip hop mix</code>\n"
        "<code>https://youtu.be/dQw4w9WgXcQ</code>\n"
        "<code>https://www.instagram.com/reel/...</code>\n"
        "<code>https://www.tiktok.com/@user/video/...</code>"
    )


def _build_results_text(
    query: str,
    videos: list[dict[str, str]],
    *,
    mode: str,
    quality: str,
    audio_bitrate: str,
) -> str:
    settings_line = (
        f"Format: <b>AUDIO {audio_bitrate}k</b>"
        if mode == "audio"
        else f"Format: <b>VIDEO {quality.upper() if quality != 'best' else 'AUTO'}</b>"
    )
    rows = [
        "<b>YouTube natijalari</b>",
        f"So'rov: <code>{html.escape(query)}</code>",
        settings_line,
        "",
        "Pastdagi tugmalar bilan yuklab oling.",
        "",
    ]
    if not videos:
        rows.append("Natija topilmadi.")
        return "\n".join(rows)

    for idx, video in enumerate(videos[:6], start=1):
        title = html.escape(video.get("title", "YouTube video"))
        uploader = html.escape(video.get("uploader", ""))
        duration = html.escape(video.get("duration", ""))
        published = html.escape(video.get("published", ""))
        rows.append(f"{idx}. <b>{title}</b>")
        meta_parts = [part for part in [uploader, duration, published] if part]
        if meta_parts:
            rows.append(f"   {' | '.join(meta_parts)}")
        rows.append("")
    return "\n".join(rows).strip()


async def _safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
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
            logger.warning("YouTube edit xatosi: %s", error)


async def _show_menu(callback: CallbackQuery, state: FSMContext) -> None:
    mode, quality, audio_bitrate = _settings(await state.get_data())
    await state.set_state(YoutubeState.waiting_input)
    await _safe_edit(
        callback,
        _prompt_text(mode, quality, audio_bitrate),
        youtube_keyboard(mode, quality, audio_bitrate),
    )


async def _record_download(
    analytics_store: AnalyticsStore,
    *,
    callback_or_message_user: object,
    source: str,
    size: int,
) -> None:
    user = callback_or_message_user
    await analytics_store.record_download(
        user_id=int(getattr(user, "id")),
        username=str(getattr(user, "username", "") or "").strip(),
        full_name=" ".join(
            part
            for part in [
                str(getattr(user, "first_name", "") or "").strip(),
                str(getattr(user, "last_name", "") or "").strip(),
            ]
            if part
        ).strip(),
        source=source,
        size=size,
    )


def _public_youtube_error(error: Exception, *, action: str) -> str:
    message = str(error or "").strip()
    lowered = message.lower()
    if isinstance(error, ValueError) and message:
        return message
    if "playlist" in lowered:
        return "Playlist emas, bitta video link yuboring."
    if "audio" in lowered and "topilmadi" in lowered:
        return "Tanlangan audio sifati topilmadi. Boshqa bitrate tanlang."
    if "video" in lowered and "topilmadi" in lowered:
        return "Tanlangan video sifati topilmadi. Boshqa sifat tanlang."
    if "limit" in lowered or "katta" in lowered:
        return "Tanlangan fayl limitdan katta."
    if action == "search":
        return "YouTube qidiruvi hozir ishlamayapti. Birozdan keyin qayta urinib ko'ring."
    return "YouTube yuklab bo'lmadi. Boshqa link yoki sifatni sinab ko'ring."


def _public_social_error(error: Exception) -> str:
    message = str(error or "").strip()
    lowered = message.lower()
    if isinstance(error, ValueError) and message:
        return message
    if "private" in lowered or "login" in lowered or "sign in" in lowered:
        return "Private yoki cheklangan video yuborildi. Public link yuboring."
    if "video topilmadi" in lowered:
        return "Videoni topib bo'lmadi. Reel yoki TikTok video link yuboring."
    if "limit" in lowered or "katta" in lowered:
        return "Tanlangan fayl limitdan katta."
    return "Instagram yoki TikTok videoni yuklab bo'lmadi. Public video link yuboring."


async def _download_and_send_youtube(
    message: Message,
    url: str,
    *,
    mode: str,
    quality: str,
    audio_bitrate: str,
    title: str,
    reply_markup: InlineKeyboardMarkup,
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
        logger.warning("YouTube yuklash xatosi: %s", error)
        with contextlib.suppress(TelegramBadRequest):
            await progress_message.edit_text(
                f"<b>YouTube xatosi</b>\n{html.escape(_public_youtube_error(error, action='download'))}",
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        raise
    finally:
        if succeeded:
            with contextlib.suppress(TelegramBadRequest):
                await progress_message.delete()
        await cleanup_download(downloaded)


async def _download_and_send_social(
    message: Message,
    url: str,
    *,
    title: str,
    reply_markup: InlineKeyboardMarkup,
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
        logger.warning("Social video yuklash xatosi: %s", error)
        with contextlib.suppress(TelegramBadRequest):
            await progress_message.edit_text(
                f"<b>Social video xatosi</b>\n{html.escape(_public_social_error(error))}",
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        raise
    finally:
        if succeeded:
            with contextlib.suppress(TelegramBadRequest):
                await progress_message.delete()
        await cleanup_download(downloaded)


@router.callback_query(F.data == "services:youtube")
async def youtube_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(
        youtube_mode="video",
        youtube_quality="best",
        youtube_audio_bitrate="192",
        youtube_results=[],
        youtube_query="",
    )
    await callback.answer()
    await _show_menu(callback, state)


@router.callback_query(F.data == "youtube:clear")
async def youtube_clear_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(youtube_results=[], youtube_query="")
    await callback.answer("Natijalar tozalandi")
    await _show_menu(callback, state)


@router.callback_query(F.data.startswith("youtube:mode:"))
async def youtube_mode_handler(callback: CallbackQuery, state: FSMContext) -> None:
    value = str((callback.data or "").split(":")[-1]).lower()
    if value not in {"video", "audio"}:
        await callback.answer("Rejim noto'g'ri", show_alert=True)
        return
    await state.update_data(youtube_mode=value)
    await callback.answer("Rejim saqlandi")
    await _show_menu(callback, state)


@router.callback_query(F.data.startswith("youtube:quality:"))
async def youtube_quality_handler(callback: CallbackQuery, state: FSMContext) -> None:
    value = str((callback.data or "").split(":")[-1]).lower()
    if value not in VIDEO_QUALITIES:
        await callback.answer("Sifat noto'g'ri", show_alert=True)
        return
    await state.update_data(youtube_quality=value)
    await callback.answer("Sifat saqlandi")
    await _show_menu(callback, state)


@router.callback_query(F.data.startswith("youtube:bitrate:"))
async def youtube_bitrate_handler(callback: CallbackQuery, state: FSMContext) -> None:
    value = str((callback.data or "").split(":")[-1]).lower()
    if value not in AUDIO_BITRATES:
        await callback.answer("Bitrate noto'g'ri", show_alert=True)
        return
    await state.update_data(youtube_audio_bitrate=value)
    await callback.answer("Bitrate saqlandi")
    await _show_menu(callback, state)


@router.message(YoutubeState.waiting_input, F.text)
async def youtube_input_handler(
    message: Message,
    state: FSMContext,
    analytics_store: AnalyticsStore,
    ai_store: AIStore,
) -> None:
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    mode, quality, audio_bitrate = _settings(await state.get_data())
    try:
        candidate_url = extract_first_url(text)
    except ValueError:
        candidate_url = ""

    if candidate_url and is_youtube_url(candidate_url):
        service_key = (
            "youtube_download_video" if mode == "video" else "youtube_download_audio"
        )
        charge = await ensure_balance(
            ai_store,
            message,
            service_key,
            reply_markup=youtube_keyboard(mode, quality, audio_bitrate),
        )
        if charge is None:
            return
        _user, cost, user_id, username, full_name = charge
        try:
            downloaded = await _download_and_send_youtube(
                message,
                candidate_url,
                mode=mode,
                quality=quality,
                audio_bitrate=audio_bitrate,
                title="YouTube",
                reply_markup=youtube_keyboard(mode, quality, audio_bitrate),
            )
        except Exception:
            return
        await finalize_charge(
            ai_store,
            service_key=service_key,
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )

        if message.from_user is not None:
            await _record_download(
                analytics_store,
                callback_or_message_user=message.from_user,
                source=downloaded.source,
                size=downloaded.size,
            )
        await state.set_state(YoutubeState.waiting_input)
        return
    if candidate_url and is_social_video_url(candidate_url):
        if mode != "video":
            await message.answer(
                (
                    "<b>Instagram/TikTok faqat video rejimida ishlaydi.</b>\n"
                    "Iltimos, rejimni `Video` ga qaytaring."
                ),
                parse_mode="HTML",
                reply_markup=youtube_keyboard(mode, quality, audio_bitrate),
            )
            return
        platform = social_platform_name(candidate_url)
        charge = await ensure_balance(
            ai_store,
            message,
            "social_download",
            reply_markup=youtube_keyboard(mode, quality, audio_bitrate),
        )
        if charge is None:
            return
        _user, cost, user_id, username, full_name = charge
        try:
            downloaded = await _download_and_send_social(
                message,
                candidate_url,
                title=platform,
                reply_markup=youtube_keyboard(mode, quality, audio_bitrate),
            )
        except Exception:
            return
        await finalize_charge(
            ai_store,
            service_key="social_download",
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )

        if message.from_user is not None:
            await _record_download(
                analytics_store,
                callback_or_message_user=message.from_user,
                source=downloaded.source,
                size=downloaded.size,
            )
        await state.set_state(YoutubeState.waiting_input)
        return

    charge = await ensure_balance(
        ai_store,
        message,
        "youtube_search",
        reply_markup=youtube_keyboard(mode, quality, audio_bitrate),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            result = await search_youtube(text, limit=6)
    except Exception as error:  # noqa: BLE001
        logger.warning("YouTube qidiruv xatosi: %s", error)
        await message.answer(
            f"<b>YouTube qidiruv xatosi</b>\n{html.escape(_public_youtube_error(error, action='search'))}",
            parse_mode="HTML",
            reply_markup=youtube_keyboard(mode, quality, audio_bitrate),
        )
        return

    videos = list(result.get("videos", []))
    await state.update_data(youtube_results=videos, youtube_query=text)
    await state.set_state(YoutubeState.waiting_input)
    await message.answer(
        _build_results_text(
            text,
            videos,
            mode=mode,
            quality=quality,
            audio_bitrate=audio_bitrate,
        ),
        parse_mode="HTML",
        reply_markup=youtube_results_keyboard(
            videos,
            mode=mode,
            quality=quality,
            audio_bitrate=audio_bitrate,
        ),
    )
    await finalize_charge(
        ai_store,
        service_key=service_key,
        user_id=user_id,
        username=username,
        full_name=full_name,
        amount=cost,
    )


@router.callback_query(F.data.startswith("youtube:download:"))
async def youtube_download_callback(
    callback: CallbackQuery,
    state: FSMContext,
    analytics_store: AnalyticsStore,
    ai_store: AIStore,
) -> None:
    if callback.message is None:
        await callback.answer("Xabar topilmadi", show_alert=True)
        return

    raw = callback.data or ""
    parts = raw.split(":")
    if len(parts) != 3:
        await callback.answer("Video topilmadi", show_alert=True)
        return
    try:
        index = int(parts[2])
    except ValueError:
        await callback.answer("Video topilmadi", show_alert=True)
        return

    data = await state.get_data()
    videos = data.get("youtube_results")
    if not isinstance(videos, list) or index < 0 or index >= len(videos):
        await callback.answer("Natijalar eskirdi. Qayta qidiring.", show_alert=True)
        return

    video = videos[index]
    if not isinstance(video, dict):
        await callback.answer("Video topilmadi", show_alert=True)
        return

    url = str(video.get("url", "")).strip()
    title = str(video.get("title", "")).strip() or f"YouTube {index + 1}"
    if not url:
        await callback.answer("Video linki topilmadi", show_alert=True)
        return

    mode, quality, audio_bitrate = _settings(data)
    service_key = (
        "youtube_download_video" if mode == "video" else "youtube_download_audio"
    )
    charge = await ensure_balance(
        ai_store,
        callback,
        service_key,
        reply_markup=youtube_results_keyboard(
            videos,
            mode=mode,
            quality=quality,
            audio_bitrate=audio_bitrate,
        ),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    await callback.answer("Yuklanmoqda...")
    try:
        downloaded = await _download_and_send_youtube(
            callback.message,
            url,
            mode=mode,
            quality=quality,
            audio_bitrate=audio_bitrate,
            title=title,
            reply_markup=youtube_results_keyboard(
                videos,
                mode=mode,
                quality=quality,
                audio_bitrate=audio_bitrate,
            ),
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
        await _record_download(
            analytics_store,
            callback_or_message_user=callback.from_user,
            source=downloaded.source,
            size=downloaded.size,
        )


@router.message(YoutubeState.waiting_input)
async def youtube_fallback(message: Message, state: FSMContext) -> None:
    mode, quality, audio_bitrate = _settings(await state.get_data())
    await message.answer(
        "YouTube qidiruv matni yoki YouTube, Instagram, TikTok link yuboring.",
        reply_markup=youtube_keyboard(mode, quality, audio_bitrate),
    )
