import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0, size))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def services_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Saqlash", callback_data="services:save"),
                InlineKeyboardButton(text="YouTube", callback_data="services:youtube"),
            ],
            [
                InlineKeyboardButton(text="Ob-havo", callback_data="services:weather"),
                InlineKeyboardButton(text="Valyuta", callback_data="services:currency"),
            ],
            [
                InlineKeyboardButton(text="Konvertor", callback_data="services:converter"),
                InlineKeyboardButton(text="Link qisqartirish", callback_data="services:tinyurl"),
            ],
            [
                InlineKeyboardButton(text="Pochta", callback_data="services:tempmail"),
                InlineKeyboardButton(text="Tarjimon", callback_data="services:translate"),
            ],
            [
                InlineKeyboardButton(text="Musiqa qidirish", callback_data="services:shazam"),
                InlineKeyboardButton(text="Ish qidirish", callback_data="services:jobs"),
            ],
            [
                InlineKeyboardButton(text="Maqola qidirish", callback_data="services:wikipedia"),
                InlineKeyboardButton(text="Fonni olib tashlash", callback_data="services:rembg"),
            ],
            [
                InlineKeyboardButton(text="Rasm yaratish", callback_data="services:pollinations")
            ],
        ]
    )


def main_menu_text(upload_limit_bytes: int, download_limit_bytes: int) -> str:
    return (
        "<b>Xizmatlar menyusi</b>\n"
        "Kerakli bo'limni tanlang.\n\n"
        "<b>Saqlash</b> - link yoki YouTube videoni chatga olib keladi\n"
        "<b>YouTube</b> - kanal ichida qidirish va natijadan yuklash\n"
        "<b>Ob-havo</b> - shahar bo'yicha ob-havo\n"
        "<b>Valyuta</b> - asosiy kurslar\n"
        "<b>Konvertor</b> - fayl va rasm formatini almashtirish\n"
        "<b>Pochta</b> - vaqtinchalik email ochish\n"
        "<b>Link qisqartirish</b> - URL ni qisqartirish\n"
        "<b>Musiqa qidirish</b> - qo'shiq nomini topish\n"
        "<b>Tarjimon</b> - tezkor tarjima\n"
        "<b>Ish qidirish</b> - vakansiyalarni topish\n"
        "<b>Maqola qidirish</b> - qisqa ensiklopediya javobi\n"
        "<b>Fonni olib tashlash</b> - rasm fonini tozalash\n"
        "<b>Rasm yaratish</b> - promptdan rasm chizish"
    )


async def safe_edit_menu(
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
            logger.warning("Menu edit failed: %s", error)
