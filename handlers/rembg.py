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
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.types.input_file import BufferedInputFile
from aiogram.utils.chat_action import ChatActionSender

from services.rembg_client import remove_background

router = Router(name="rembg")
logger = logging.getLogger(__name__)
REMBG_TMP_DIR = Path(__file__).resolve().parent.parent / "downloads_tmp" / "rembg"
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


class RembgState(StatesGroup):
    waiting_image = State()


def rembg_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")]
        ]
    )


def rembg_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yana fon olib tashlash", callback_data="rembg:start")],
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
            logger.warning("Rembg edit xatosi: %s", error)


def _make_work_dir() -> Path:
    REMBG_TMP_DIR.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="rembg_", dir=str(REMBG_TMP_DIR)))


def _is_supported_document(document: Document) -> bool:
    mime = str(document.mime_type or "").lower()
    if mime.startswith("image/"):
        return True
    extension = Path(str(document.file_name or "")).suffix.lower()
    return extension in SUPPORTED_IMAGE_EXTENSIONS


async def _download_image(message: Message, work_dir: Path) -> Path:
    if message.photo:
        photo = message.photo[-1]
        output = work_dir / f"photo_{photo.file_unique_id[:8]}.jpg"
        await message.bot.download(photo, destination=output)
        if not output.exists():
            raise RuntimeError("Rasmni yuklab bo'lmadi.")
        return output

    document = message.document
    if document is None:
        raise ValueError("Rasm yuborilmadi.")
    if not _is_supported_document(document):
        allowed = ", ".join(sorted(SUPPORTED_IMAGE_EXTENSIONS))
        raise ValueError(f"Rasm turi noto'g'ri. Ruxsat etilgan: {allowed}")

    extension = Path(str(document.file_name or "image.png")).suffix.lower() or ".png"
    output = work_dir / f"document_{document.file_unique_id[:8]}{extension}"
    await message.bot.download(document, destination=output)
    if not output.exists():
        raise RuntimeError("Rasmni yuklab bo'lmadi.")
    return output


async def _show_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RembgState.waiting_image)
    await _safe_edit(
        callback,
        (
            "<b>Rembg (Background Remover)</b>\n"
            "Rasm yuboring, bot fonni olib tashlab PNG qaytaradi."
        ),
        rembg_prompt_keyboard(),
    )


@router.callback_query(F.data == "services:rembg")
async def rembg_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _show_prompt(callback, state)


@router.callback_query(F.data == "rembg:start")
async def rembg_start_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_prompt(callback, state)


@router.message(RembgState.waiting_image, F.photo | F.document)
async def rembg_image_handler(message: Message, state: FSMContext) -> None:
    work_dir = _make_work_dir()
    try:
        source = await _download_image(message, work_dir)
        raw = source.read_bytes()
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            output_png = await remove_background(raw)

        filename = f"{source.stem}_nobg.png"
        out_file = BufferedInputFile(output_png, filename=filename)
        await message.answer_document(
            out_file,
            caption="Fon olib tashlandi (PNG).",
            reply_markup=rembg_result_keyboard(),
        )
        await state.set_state(RembgState.waiting_image)
    except Exception as error:  # noqa: BLE001
        await message.answer(
            f"<b>Rembg xatosi</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=rembg_prompt_keyboard(),
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@router.message(RembgState.waiting_image)
async def rembg_fallback(message: Message) -> None:
    await message.answer(
        "Rasm yuboring (photo yoki image document).",
        reply_markup=rembg_prompt_keyboard(),
    )
