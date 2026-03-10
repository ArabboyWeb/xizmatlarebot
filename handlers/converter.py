import asyncio
import html
import logging
import shutil
import tempfile
from pathlib import Path

import fitz
from PIL import UnidentifiedImageError
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
from services.converter_tools import (
    convert_with_soffice,
    image_format_sync,
    image_to_pdf_sync,
    pdf_to_images_zip_sync,
)
from services.token_billing import ensure_balance

router = Router(name="converter")
logger = logging.getLogger(__name__)

CONVERTER_TMP_DIR = (
    Path(__file__).resolve().parent.parent / "downloads_tmp" / "converter"
)
SUPPORTED_WORD_EXTENSIONS = {".doc", ".docx", ".odt", ".rtf"}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
MAX_PDF_PAGES = 40
CONVERTER_TIMEOUT_SECONDS = 180


class ConverterState(StatesGroup):
    waiting_word_to_pdf = State()
    waiting_pdf_to_word = State()
    waiting_image_to_pdf = State()
    waiting_pdf_to_images = State()
    waiting_image_format = State()


def converter_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Word -> PDF", callback_data="converter:word_to_pdf"
                ),
                InlineKeyboardButton(
                    text="PDF -> Word", callback_data="converter:pdf_to_word"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Image -> PDF", callback_data="converter:image_to_pdf"
                ),
                InlineKeyboardButton(
                    text="PDF -> Images", callback_data="converter:pdf_to_images"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Image format", callback_data="converter:image_format"
                )
            ],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")]
        ]
    )


def image_format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="PNG", callback_data="converter:image_format:png"
                ),
                InlineKeyboardButton(
                    text="JPG", callback_data="converter:image_format:jpg"
                ),
                InlineKeyboardButton(
                    text="WEBP", callback_data="converter:image_format:webp"
                ),
            ],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


def result_keyboard(retry_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yana", callback_data=retry_callback)],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


def _sanitize_stem(file_name: str) -> str:
    base = Path(file_name).stem
    safe = "".join(ch for ch in base if ch.isalnum() or ch in {"_", "-"}).strip("_-")
    return safe[:80] or "file"


def _make_work_dir() -> Path:
    CONVERTER_TMP_DIR.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="conv_", dir=str(CONVERTER_TMP_DIR)))


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
            logger.warning("Converter edit xatosi: %s", error)


async def _download_document(
    message: Message, work_dir: Path, allowed_extensions: set[str]
) -> Path:
    if message.document is None:
        raise ValueError("Fayl yuborilmadi.")
    file_name = message.document.file_name or "file"
    extension = Path(file_name).suffix.lower()
    if extension not in allowed_extensions:
        allowed = ", ".join(sorted(allowed_extensions))
        raise ValueError(f"Noto'g'ri format. Ruxsat etilgan formatlar: {allowed}")

    output = (
        work_dir
        / f"{_sanitize_stem(file_name)}_{message.document.file_unique_id[:8]}{extension}"
    )
    await message.bot.download(message.document, destination=output)
    if not output.exists():
        raise RuntimeError("Faylni yuklab bo'lmadi.")
    return output


async def _download_image(message: Message, work_dir: Path) -> Path:
    if message.photo:
        photo = message.photo[-1]
        output = work_dir / f"photo_{photo.file_unique_id[:8]}.jpg"
        await message.bot.download(photo, destination=output)
        if not output.exists():
            raise RuntimeError("Rasmni yuklab bo'lmadi.")
        return output

    if message.document is None:
        raise ValueError("Rasm yuborilmadi.")

    file_name = message.document.file_name or "image"
    extension = Path(file_name).suffix.lower()
    if extension not in SUPPORTED_IMAGE_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_IMAGE_EXTENSIONS))
        raise ValueError(
            f"Rasm formati qo'llab-quvvatlanmadi. Ruxsat etilgan: {allowed}"
        )

    output = (
        work_dir
        / f"{_sanitize_stem(file_name)}_{message.document.file_unique_id[:8]}{extension}"
    )
    await message.bot.download(message.document, destination=output)
    if not output.exists():
        raise RuntimeError("Rasmni yuklab bo'lmadi.")
    return output


async def _answer_conversion_error(
    message: Message, error: Exception, retry_callback: str
) -> None:
    await message.answer(
        f"<b>Konvertatsiya xatosi</b>\n{html.escape(str(error))}",
        parse_mode="HTML",
        reply_markup=result_keyboard(retry_callback),
    )


async def _handle_word_to_pdf(message: Message, ai_store: AIStore) -> None:
    charge = await ensure_balance(
        ai_store,
        message,
        "converter_word_to_pdf",
        reply_markup=result_keyboard("converter:word_to_pdf"),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    work_dir = _make_work_dir()
    try:
        source = await _download_document(message, work_dir, SUPPORTED_WORD_EXTENSIONS)
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            output = await convert_with_soffice(
                source, "pdf", CONVERTER_TIMEOUT_SECONDS
            )
        await message.answer_document(
            document=FSInputFile(output),
            caption="Word fayl PDF ga konvert qilindi.",
            reply_markup=result_keyboard("converter:word_to_pdf"),
        )
    except (ValueError, RuntimeError, TimeoutError, OSError) as error:
        await _answer_conversion_error(message, error, "converter:word_to_pdf")
    except Exception as error:  # noqa: BLE001
        logger.exception("Word -> PDF konvertatsiyasida kutilmagan xatolik")
        await _answer_conversion_error(message, error, "converter:word_to_pdf")
    else:
        await ai_store.charge_tokens(
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def _handle_pdf_to_word(message: Message, ai_store: AIStore) -> None:
    charge = await ensure_balance(
        ai_store,
        message,
        "converter_pdf_to_word",
        reply_markup=result_keyboard("converter:pdf_to_word"),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    work_dir = _make_work_dir()
    try:
        source = await _download_document(message, work_dir, {".pdf"})
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            output = await convert_with_soffice(
                source, "docx", CONVERTER_TIMEOUT_SECONDS
            )
        await message.answer_document(
            document=FSInputFile(output),
            caption="PDF fayl Word (DOCX) ga konvert qilindi.",
            reply_markup=result_keyboard("converter:pdf_to_word"),
        )
    except (ValueError, RuntimeError, TimeoutError, OSError) as error:
        await _answer_conversion_error(message, error, "converter:pdf_to_word")
    except Exception as error:  # noqa: BLE001
        logger.exception("PDF -> Word konvertatsiyasida kutilmagan xatolik")
        await _answer_conversion_error(message, error, "converter:pdf_to_word")
    else:
        await ai_store.charge_tokens(
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def _handle_image_to_pdf(message: Message, ai_store: AIStore) -> None:
    charge = await ensure_balance(
        ai_store,
        message,
        "converter_image_to_pdf",
        reply_markup=result_keyboard("converter:image_to_pdf"),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    work_dir = _make_work_dir()
    try:
        source = await _download_image(message, work_dir)
        output = work_dir / f"{source.stem}.pdf"
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            await asyncio.to_thread(image_to_pdf_sync, source, output)
        await message.answer_document(
            document=FSInputFile(output),
            caption="Rasm PDF formatiga konvert qilindi.",
            reply_markup=result_keyboard("converter:image_to_pdf"),
        )
    except (
        ValueError,
        RuntimeError,
        TimeoutError,
        OSError,
        UnidentifiedImageError,
    ) as error:
        await _answer_conversion_error(message, error, "converter:image_to_pdf")
    except Exception as error:  # noqa: BLE001
        logger.exception("Image -> PDF konvertatsiyasida kutilmagan xatolik")
        await _answer_conversion_error(message, error, "converter:image_to_pdf")
    else:
        await ai_store.charge_tokens(
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def _handle_pdf_to_images(message: Message, ai_store: AIStore) -> None:
    charge = await ensure_balance(
        ai_store,
        message,
        "converter_pdf_to_images",
        reply_markup=result_keyboard("converter:pdf_to_images"),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    work_dir = _make_work_dir()
    try:
        source = await _download_document(message, work_dir, {".pdf"})
        output = work_dir / f"{source.stem}_images.zip"
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            pages = await asyncio.to_thread(
                pdf_to_images_zip_sync, source, output, MAX_PDF_PAGES
            )
        await message.answer_document(
            document=FSInputFile(output),
            caption=f"PDF rasmlarga konvert qilindi. Sahifalar: {pages}",
            reply_markup=result_keyboard("converter:pdf_to_images"),
        )
    except (
        ValueError,
        RuntimeError,
        TimeoutError,
        OSError,
        fitz.FileDataError,
    ) as error:
        await _answer_conversion_error(message, error, "converter:pdf_to_images")
    except Exception as error:  # noqa: BLE001
        logger.exception("PDF -> Images konvertatsiyasida kutilmagan xatolik")
        await _answer_conversion_error(message, error, "converter:pdf_to_images")
    else:
        await ai_store.charge_tokens(
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def _handle_image_format(
    message: Message,
    ai_store: AIStore,
    target: str,
) -> None:
    charge = await ensure_balance(
        ai_store,
        message,
        "converter_image_format",
        reply_markup=result_keyboard("converter:image_format"),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    work_dir = _make_work_dir()
    try:
        source = await _download_image(message, work_dir)
        output = work_dir / f"{source.stem}.{target}"
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            await asyncio.to_thread(image_format_sync, source, output, target)
        await message.answer_document(
            document=FSInputFile(output),
            caption=f"Rasm {target.upper()} formatiga konvert qilindi.",
            reply_markup=result_keyboard("converter:image_format"),
        )
    except (
        ValueError,
        RuntimeError,
        TimeoutError,
        OSError,
        UnidentifiedImageError,
    ) as error:
        await _answer_conversion_error(message, error, "converter:image_format")
    except Exception as error:  # noqa: BLE001
        logger.exception("Image format konvertatsiyasida kutilmagan xatolik")
        await _answer_conversion_error(message, error, "converter:image_format")
    else:
        await ai_store.charge_tokens(
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@router.callback_query(F.data == "services:converter")
async def converter_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _safe_edit(
        callback,
        (
            "<b>Converter</b>\n"
            "PDF, Word va rasm fayllarni professional konvertatsiya qiling.\n"
            "Kerakli bo'limni tanlang:"
        ),
        converter_menu_keyboard(),
    )


@router.callback_query(F.data == "converter:word_to_pdf")
async def word_to_pdf_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ConverterState.waiting_word_to_pdf)
    await callback.answer()
    await _safe_edit(
        callback,
        "<b>Word -> PDF</b>\nDOC/DOCX/ODT/RTF fayl yuboring.",
        back_keyboard(),
    )


@router.callback_query(F.data == "converter:pdf_to_word")
async def pdf_to_word_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ConverterState.waiting_pdf_to_word)
    await callback.answer()
    await _safe_edit(
        callback,
        "<b>PDF -> Word</b>\nPDF fayl yuboring.",
        back_keyboard(),
    )


@router.callback_query(F.data == "converter:image_to_pdf")
async def image_to_pdf_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ConverterState.waiting_image_to_pdf)
    await callback.answer()
    await _safe_edit(
        callback,
        "<b>Image -> PDF</b>\nRasm yuboring (photo yoki image document).",
        back_keyboard(),
    )


@router.callback_query(F.data == "converter:pdf_to_images")
async def pdf_to_images_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ConverterState.waiting_pdf_to_images)
    await callback.answer()
    await _safe_edit(
        callback,
        f"<b>PDF -> Images</b>\nPDF yuboring (maksimal {MAX_PDF_PAGES} sahifa).",
        back_keyboard(),
    )


@router.callback_query(F.data == "converter:image_format")
async def image_format_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _safe_edit(
        callback,
        "<b>Image format converter</b>\nMaqsad formatni tanlang:",
        image_format_keyboard(),
    )


@router.callback_query(F.data.startswith("converter:image_format:"))
async def image_format_target_callback(
    callback: CallbackQuery, state: FSMContext
) -> None:
    if not callback.data:
        await callback.answer()
        return
    target = callback.data.rsplit(":", 1)[-1].lower()
    if target not in {"png", "jpg", "webp"}:
        await callback.answer("Noto'g'ri format", show_alert=True)
        return
    await state.set_state(ConverterState.waiting_image_format)
    await state.update_data(image_target=target)
    await callback.answer()
    await _safe_edit(
        callback,
        f"<b>Image -> {target.upper()}</b>\nRasm yuboring (photo yoki image document).",
        back_keyboard(),
    )


@router.message(ConverterState.waiting_word_to_pdf, F.document)
async def word_to_pdf_message(message: Message, ai_store: AIStore) -> None:
    await _handle_word_to_pdf(message, ai_store)


@router.message(ConverterState.waiting_pdf_to_word, F.document)
async def pdf_to_word_message(message: Message, ai_store: AIStore) -> None:
    await _handle_pdf_to_word(message, ai_store)


@router.message(ConverterState.waiting_image_to_pdf, F.photo | F.document)
async def image_to_pdf_message(message: Message, ai_store: AIStore) -> None:
    await _handle_image_to_pdf(message, ai_store)


@router.message(ConverterState.waiting_pdf_to_images, F.document)
async def pdf_to_images_message(message: Message, ai_store: AIStore) -> None:
    await _handle_pdf_to_images(message, ai_store)


@router.message(ConverterState.waiting_image_format, F.photo | F.document)
async def image_format_message(
    message: Message,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    data = await state.get_data()
    target = str(data.get("image_target", "")).lower()
    if target not in {"png", "jpg", "webp"}:
        await message.answer(
            "Maqsad format topilmadi. Qayta tanlang.",
            reply_markup=image_format_keyboard(),
        )
        return
    await _handle_image_format(message, ai_store, target)


@router.message(ConverterState.waiting_word_to_pdf)
async def word_to_pdf_fallback(message: Message) -> None:
    await message.answer(
        "Word fayl yuboring (DOC, DOCX, ODT, RTF).",
        reply_markup=back_keyboard(),
    )


@router.message(ConverterState.waiting_pdf_to_word)
async def pdf_to_word_fallback(message: Message) -> None:
    await message.answer(
        "PDF fayl yuboring.",
        reply_markup=back_keyboard(),
    )


@router.message(ConverterState.waiting_image_to_pdf)
async def image_to_pdf_fallback(message: Message) -> None:
    await message.answer(
        "Rasm yuboring (photo yoki image document).",
        reply_markup=back_keyboard(),
    )


@router.message(ConverterState.waiting_pdf_to_images)
async def pdf_to_images_fallback(message: Message) -> None:
    await message.answer(
        "PDF fayl yuboring.",
        reply_markup=back_keyboard(),
    )


@router.message(ConverterState.waiting_image_format)
async def image_format_fallback(message: Message) -> None:
    await message.answer(
        "Rasm yuboring (photo yoki image document).",
        reply_markup=back_keyboard(),
    )
