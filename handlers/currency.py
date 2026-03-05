import asyncio
import html
import logging

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.chat_action import ChatActionSender

from services.currency_client import build_currency_text, fetch_currency_rates

router = Router(name="currency")
logger = logging.getLogger(__name__)


def currency_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Refresh", callback_data="currency:refresh")],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


def currency_error_text(message: str) -> str:
    safe_message = html.escape(message)
    return (
        "<b>Valyuta modulida xatolik</b>\n"
        f"{safe_message}\n\n"
        "Bir ozdan keyin qayta urinib ko'ring."
    )


async def render_currency(callback: CallbackQuery) -> None:
    if callback.message is None:
        await callback.answer()
        return

    if callback.data == "currency:refresh":
        await callback.answer("Yangilanmoqda...")
    else:
        await callback.answer()

    try:
        async with ChatActionSender.typing(
            bot=callback.bot, chat_id=callback.message.chat.id
        ):
            rates, date_value = await fetch_currency_rates()
        text = build_currency_text(rates, date_value)
    except asyncio.TimeoutError:
        text = currency_error_text("So'rov vaqti tugadi (timeout).")
    except aiohttp.ClientError as error:
        logger.exception("CBU API so'rovida tarmoq xatosi")
        text = currency_error_text(f"Tarmoq xatosi: {error}")
    except Exception as error:  # noqa: BLE001
        logger.exception("CBU modulida kutilmagan xatolik")
        text = currency_error_text(f"Kutilmagan xatolik: {error}")

    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=currency_keyboard(),
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in (error.message or "").lower():
            logger.warning("Currency message edit xatosi: %s", error)


@router.callback_query(F.data == "services:currency")
async def currency_menu_handler(callback: CallbackQuery) -> None:
    await render_currency(callback)


@router.callback_query(F.data == "currency:refresh")
async def currency_refresh_handler(callback: CallbackQuery) -> None:
    await render_currency(callback)
