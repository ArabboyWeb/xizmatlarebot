import html
import logging
import shutil
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Audio,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Voice,
)
from aiogram.utils.chat_action import ChatActionSender

from services.shazam_client import recognize_track

router = Router(name="shazam")
logger = logging.getLogger(__name__)

SHZ_TMP_DIR = Path(__file__).resolve().parent.parent / "downloads_tmp" / "shazam"
SUPPORTED_AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".mp4",
}


class ShazamState(StatesGroup):
    waiting_audio = State()


def shazam_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")]
        ]
    )


def shazam_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yana aniqlash", callback_data="shazam:start")],
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


def _make_work_dir() -> Path:
    SHZ_TMP_DIR.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="shz_", dir=str(SHZ_TMP_DIR)))


def _document_audio_extension(document: Document) -> str:
    name = str(document.file_name or "").strip().lower()
    return Path(name).suffix


def _is_supported_document(document: Document) -> bool:
    mime = str(document.mime_type or "").lower()
    if mime.startswith("audio/"):
        return True
    return _document_audio_extension(document) in SUPPORTED_AUDIO_EXTENSIONS


async def _download_audio_file(message: Message, work_dir: Path) -> Path:
    if message.audio is not None:
        audio: Audio = message.audio
        extension = Path(str(audio.file_name or "audio.m4a")).suffix or ".m4a"
        output = work_dir / f"audio_{audio.file_unique_id[:8]}{extension.lower()}"
        await message.bot.download(audio, destination=output)
        return output

    if message.voice is not None:
        voice: Voice = message.voice
        output = work_dir / f"voice_{voice.file_unique_id[:8]}.ogg"
        await message.bot.download(voice, destination=output)
        return output

    if message.document is not None:
        document = message.document
        if not _is_supported_document(document):
            allowed = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
            raise ValueError(
                f"Audio fayl turi noto'g'ri. Ruxsat etilgan kengaytmalar: {allowed}"
            )
        extension = _document_audio_extension(document) or ".mp3"
        output = work_dir / f"doc_{document.file_unique_id[:8]}{extension.lower()}"
        await message.bot.download(document, destination=output)
        return output

    raise ValueError("Audio yuborilmadi.")


async def _show_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ShazamState.waiting_audio)
    await _safe_edit(
        callback,
        (
            "<b>ShazamIO (Free)</b>\n"
            "Audio, voice yoki music fayl yuboring, bot trekni aniqlaydi."
        ),
        shazam_prompt_keyboard(),
    )


@router.callback_query(F.data == "services:shazam")
async def shazam_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _show_prompt(callback, state)


@router.callback_query(F.data == "shazam:start")
async def shazam_start_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_prompt(callback, state)


@router.message(ShazamState.waiting_audio, F.audio | F.voice | F.document)
async def shazam_audio_handler(message: Message, state: FSMContext) -> None:
    work_dir = _make_work_dir()
    try:
        source = await _download_audio_file(message, work_dir)
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            track = await recognize_track(source)

        result_text = (
            "<b>Trek topildi</b>\n"
            f"Title: <b>{html.escape(track.get('title', 'Nomalum'))}</b>\n"
            f"Artist: <b>{html.escape(track.get('artist', 'Nomalum'))}</b>"
        )
        album = track.get("album", "")
        genre = track.get("genre", "")
        url = track.get("url", "")
        if album:
            result_text += f"\nAlbum: <b>{html.escape(album)}</b>"
        if genre:
            result_text += f"\nJanr: <b>{html.escape(genre)}</b>"
        if url:
            result_text += f"\nLink: {html.escape(url)}"

        cover = str(track.get("cover", "")).strip()
        if cover:
            await message.answer_photo(
                cover,
                caption=result_text,
                parse_mode="HTML",
                reply_markup=shazam_result_keyboard(),
            )
        else:
            await message.answer(
                result_text,
                parse_mode="HTML",
                reply_markup=shazam_result_keyboard(),
            )
        await state.set_state(ShazamState.waiting_audio)
    except Exception as error:  # noqa: BLE001
        await message.answer(
            f"<b>Shazam xatosi</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=shazam_prompt_keyboard(),
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@router.message(ShazamState.waiting_audio)
async def shazam_fallback(message: Message) -> None:
    await message.answer(
        "Audio/voice yoki audio document yuboring.",
        reply_markup=shazam_prompt_keyboard(),
    )
