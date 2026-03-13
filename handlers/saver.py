from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import mimetypes
import re
import subprocess
from pathlib import Path

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
from aiogram.types.input_file import FSInputFile
from aiogram.utils.chat_action import ChatActionSender

from services.ai_store import AIStore
from services.analytics_store import AnalyticsStore
from services.social_client import (
    download_social_video,
    is_social_video_url,
    social_error_public_text,
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
from services.token_billing import ensure_balance, finalize_charge
from services.youtube_client import download_youtube

try:
    import imageio_ffmpeg
except Exception:  # pragma: no cover
    imageio_ffmpeg = None

router = Router(name="saver")
logger = logging.getLogger(__name__)


class SaverState(StatesGroup):
    waiting_url = State()


TELEGRAM_STREAMABLE_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov"}
TELEGRAM_MOBILE_VIDEO_SUFFIXES = {".mp4"}
TELEGRAM_MOBILE_VIDEO_CODECS = {"h264"}
TELEGRAM_MOBILE_AUDIO_CODECS = {"", "aac", "mp3"}
TELEGRAM_MOBILE_PIXEL_FORMATS = {"yuv420p", "nv12"}


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
        "Direct fayl linkini yuboring. "
        "Bot uni yuklab, shu chatga qaytaradi.\n\n"
        f"Maksimal hajm: <b>{limit_text}</b>\n"
        "Misol:\n"
        "<code>https://example.com/file.pdf</code>\n\n"
        "YouTube/Instagram/TikTok uchun Media bo'limidagi "
        "<b>YT / Insta / TikTok Saver</b> oqimidan foydalaning."
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


def save_video_redirect_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎥 YT / Insta / TikTok Saver",
                    callback_data="services:youtube",
                )
            ],
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


def _ffmpeg_path() -> str:
    if imageio_ffmpeg is None:
        return ""
    try:
        value = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover
        return ""
    return value if value and Path(value).exists() else ""


def _probe_video_streams(path: Path, *, ffmpeg_executable: str) -> dict[str, object]:
    command = [
        ffmpeg_executable,
        "-hide_banner",
        "-i",
        str(path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    output = "\n".join(part for part in [result.stdout, result.stderr] if part)
    metadata: dict[str, object] = {
        "video_codec": "",
        "audio_codec": "",
        "pixel_format": "",
        "width": 0,
        "height": 0,
        "duration": 0,
    }
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("Duration:") and not metadata["duration"]:
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", line)
            if match:
                hours = int(match.group(1))
                minutes = int(match.group(2))
                seconds = float(match.group(3))
                metadata["duration"] = int(hours * 3600 + minutes * 60 + seconds)
        if "Video:" in line and not metadata["video_codec"]:
            payload = line.split("Video:", 1)[1].strip()
            parts = [part.strip() for part in payload.split(",") if part.strip()]
            if parts:
                metadata["video_codec"] = parts[0].split()[0].lower()
            for part in parts[1:]:
                lowered = part.lower()
                if not metadata["pixel_format"] and lowered.startswith(("yuv", "nv")):
                    metadata["pixel_format"] = lowered.split()[0].split("(")[0]
                match = re.search(r"(\d{2,5})x(\d{2,5})", part)
                if match:
                    metadata["width"] = int(match.group(1))
                    metadata["height"] = int(match.group(2))
                    break
        if "Audio:" in line and not metadata["audio_codec"]:
            payload = line.split("Audio:", 1)[1].strip()
            parts = [part.strip() for part in payload.split(",") if part.strip()]
            if parts:
                metadata["audio_codec"] = parts[0].split()[0].lower()
    return metadata


def _should_convert_video_for_telegram(
    downloaded: DownloadedFile,
    *,
    ffmpeg_executable: str,
) -> bool:
    suffix = downloaded.path.suffix.lower()
    content_type = str(downloaded.content_type or "").strip().lower()
    if suffix not in TELEGRAM_STREAMABLE_VIDEO_EXTENSIONS:
        return True
    if not content_type.startswith("video/"):
        return True
    if suffix not in TELEGRAM_MOBILE_VIDEO_SUFFIXES:
        return True

    metadata = _probe_video_streams(downloaded.path, ffmpeg_executable=ffmpeg_executable)
    video_codec = str(metadata.get("video_codec", "") or "").lower()
    audio_codec = str(metadata.get("audio_codec", "") or "").lower()
    pixel_format = str(metadata.get("pixel_format", "") or "").lower()
    width = int(metadata.get("width", 0) or 0)
    height = int(metadata.get("height", 0) or 0)

    if video_codec not in TELEGRAM_MOBILE_VIDEO_CODECS:
        return True
    if audio_codec not in TELEGRAM_MOBILE_AUDIO_CODECS:
        return True
    if pixel_format and pixel_format not in TELEGRAM_MOBILE_PIXEL_FORMATS:
        return True
    if width > 0 and height > 0 and (width % 2 != 0 or height % 2 != 0):
        return True
    return False


def _convert_video_to_mp4_sync(
    downloaded: DownloadedFile,
    *,
    ffmpeg_executable: str,
) -> DownloadedFile:
    source = downloaded.path
    if not source.exists():
        return downloaded
    target = (
        source.with_name(f"{source.stem}_tg.mp4")
        if source.suffix.lower() == ".mp4"
        else source.with_suffix(".mp4")
    )
    if target.exists():
        with contextlib.suppress(OSError):
            target.unlink()
    source_meta = _probe_video_streams(source, ffmpeg_executable=ffmpeg_executable)
    has_audio = bool(str(source_meta.get("audio_codec", "") or "").strip())

    command = [
        ffmpeg_executable,
        "-y",
        "-i",
        str(source),
    ]
    if not has_audio:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
            ]
        )
    command.extend(
        [
            "-map",
            "0:v:0",
            "-map",
            ("0:a:0?" if has_audio else "1:a:0"),
        ]
    )
    if not has_audio:
        command.append("-shortest")
    command.extend(
        [
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-sn",
        "-dn",
        "-vf",
        (
            "scale="
            "'if(gt(iw,ih),min(1280,iw),-2)':"
            "'if(gt(iw,ih),-2,min(1280,ih))',"
            "setsar=1,format=yuv420p"
        ),
        "-fps_mode",
        "cfr",
        "-r",
        "30",
        "-movflags",
        "+faststart",
        "-brand",
        "mp42",
        "-c:v",
        "libx264",
        "-tag:v",
        "avc1",
        "-profile:v",
        "baseline",
        "-level",
        "3.1",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-maxrate",
        "2500k",
        "-bufsize",
        "5000k",
        "-g",
        "60",
        "-keyint_min",
        "60",
        "-sc_threshold",
        "0",
        "-bf",
        "0",
        "-c:a",
        "aac",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-af",
        "aresample=async=1:first_pts=0",
        "-b:a",
        "128k",
        "-max_muxing_queue_size",
        "1024",
        str(target),
        ]
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=False,
        check=False,
    )
    if result.returncode != 0 or not target.exists() or target.stat().st_size <= 0:
        logger.warning("Video MP4 normalize xatosi: ffmpeg returncode=%s", result.returncode)
        with contextlib.suppress(OSError):
            target.unlink()
        return downloaded

    if source != target:
        with contextlib.suppress(OSError):
            source.unlink()
    return DownloadedFile(
        path=target,
        file_name=target.name,
        size=int(target.stat().st_size),
        content_type="video/mp4",
        source=downloaded.source,
    )


async def _prepare_video_for_send(downloaded: DownloadedFile) -> DownloadedFile:
    ffmpeg_executable = _ffmpeg_path()
    if not ffmpeg_executable:
        return downloaded
    # Mobile Telegram playback is more fragile than desktop, so normalize every video
    # to one safe MP4 profile before sending.
    converted = await asyncio.to_thread(
        _convert_video_to_mp4_sync,
        downloaded,
        ffmpeg_executable=ffmpeg_executable,
    )
    return converted


def _video_send_params(downloaded: DownloadedFile) -> dict[str, int]:
    ffmpeg_executable = _ffmpeg_path()
    if not ffmpeg_executable:
        return {}
    meta = _probe_video_streams(downloaded.path, ffmpeg_executable=ffmpeg_executable)
    params: dict[str, int] = {}
    width = int(meta.get("width", 0) or 0)
    height = int(meta.get("height", 0) or 0)
    duration = int(meta.get("duration", 0) or 0)
    if width > 0:
        params["width"] = width
    if height > 0:
        params["height"] = height
    if duration > 0:
        params["duration"] = duration
    return params


async def send_downloaded_file(
    message: Message,
    downloaded: DownloadedFile,
    *,
    title: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    def build_caption(item: DownloadedFile) -> str:
        return (
            "<b>Fayl tayyor</b>\n"
            f"Nomi: <code>{html.escape(item.file_name)}</code>\n"
            f"Hajmi: <b>{_format_bytes(item.size)}</b>\n"
            f"Manba: <b>{html.escape(title)}</b>"
        )

    file_input = FSInputFile(downloaded.path, filename=downloaded.file_name)
    caption = build_caption(downloaded)
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
            downloaded = await _prepare_video_for_send(downloaded)
            send_kind = detect_send_kind(downloaded.file_name, downloaded.content_type)
            file_input = FSInputFile(downloaded.path, filename=downloaded.file_name)
            caption = build_caption(downloaded)
            video_params = _video_send_params(downloaded)
            await message.answer_video(
                video=file_input,
                caption=caption,
                parse_mode="HTML",
                supports_streaming=True,
                **video_params,
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
        return "Audio format topilmadi."
    if "video" in lowered and "topilmadi" in lowered:
        return "Video format topilmadi."
    if "limit" in lowered or "katta" in lowered:
        return "Fayl limitdan katta."
    return "YouTube linkini yuklab bo'lmadi. Boshqa linkni sinab ko'ring."


def _public_social_save_error(error: Exception) -> str:
    return social_error_public_text(error)


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


@router.message(Command("save"))
async def save_command_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SaverState.waiting_url)
    await message.answer(
        save_prompt_text(),
        parse_mode="HTML",
        reply_markup=save_keyboard(),
    )


@router.message(SaverState.waiting_url, F.text & ~F.text.startswith("/"))
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
                "<b>YouTube/Instagram/TikTok videolar bitta oqimga o'tkazilgan.</b>\n"
                "Iltimos, <b>YT / Insta / TikTok Saver</b> bo'limidan foydalaning."
            ),
            parse_mode="HTML",
            reply_markup=save_video_redirect_keyboard(),
        )
        return
    if candidate_url and is_social_video_url(candidate_url):
        await message.answer(
            (
                "<b>YouTube/Instagram/TikTok videolar bitta oqimga o'tkazilgan.</b>\n"
                "Iltimos, <b>YT / Insta / TikTok Saver</b> bo'limidan foydalaning."
            ),
            parse_mode="HTML",
            reply_markup=save_video_redirect_keyboard(),
        )
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
    await finalize_charge(
        ai_store,
        service_key="save_direct",
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
    _ = analytics_store, ai_store
    await callback.answer()
    await callback.message.answer(
        (
            "<b>YouTube/Instagram/TikTok videolar bitta oqimga o'tkazilgan.</b>\n"
            "Iltimos, <b>YT / Insta / TikTok Saver</b> bo'limidan foydalaning."
        ),
        parse_mode="HTML",
        reply_markup=save_video_redirect_keyboard(),
    )
    await state.update_data(save_youtube_url="")
    await state.set_state(SaverState.waiting_url)


@router.message(SaverState.waiting_url)
async def save_fallback_handler(message: Message) -> None:
    await message.answer(
        "Link yuboring.",
        reply_markup=save_keyboard(),
    )
