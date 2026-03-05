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
                InlineKeyboardButton(text="Ob-havo", callback_data="services:weather"),
                InlineKeyboardButton(text="Valyuta", callback_data="services:currency"),
            ],
            [
                InlineKeyboardButton(
                    text="Konvertor", callback_data="services:converter"
                )
            ],
            [
                InlineKeyboardButton(text="1secmail", callback_data="services:tempmail"),
                InlineKeyboardButton(text="TinyURL", callback_data="services:tinyurl"),
            ],
            [
                InlineKeyboardButton(text="ShazamIO", callback_data="services:shazam"),
                InlineKeyboardButton(
                    text="Tarjimon", callback_data="services:translate"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Wikipedia", callback_data="services:wikipedia"
                ),
                InlineKeyboardButton(text="Rembg", callback_data="services:rembg"),
            ],
            [
                InlineKeyboardButton(
                    text="Pollinations AI", callback_data="services:pollinations"
                )
            ],
        ]
    )


def main_menu_text(upload_limit_bytes: int, download_limit_bytes: int) -> str:
    return (
        "<b>Asosiy menu</b>\n"
        "Kerakli xizmatni tanlang.\n\n"
        "<b>Ob-havo</b> - shahar yoki lokatsiya boyicha ob-havo\n"
        "<b>Valyuta</b> - songgi kurslar\n"
        "<b>Konvertor</b> - hujjat va rasm konvertatsiyasi\n"
        "<b>1secmail</b> - temporary email inbox\n"
        "<b>TinyURL</b> - uzun linkni qisqartirish\n"
        "<b>ShazamIO</b> - audio trekni aniqlash\n"
        "<b>Tarjimon</b> - Googletrans/LibreTranslate\n"
        "<b>Wikipedia</b> - tezkor ensiklopediya qidiruvi\n"
        "<b>Rembg</b> - rasm fonini olib tashlash\n"
        "<b>Pollinations AI</b> - AI rasm generatsiya\n\n"
        f"Telegram free upload limiti: <b>{_format_bytes(upload_limit_bytes)}</b>\n"
        f"Telegram free download limiti: <b>{_format_bytes(download_limit_bytes)}</b>"
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
