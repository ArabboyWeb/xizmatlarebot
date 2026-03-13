import asyncio
import html
import logging

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from services.ai_store import AIStore
from services.request_feedback import clear_wait_message, edit_wait_message, send_wait_message
from services.token_billing import ensure_balance
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


async def render_currency(callback: CallbackQuery, ai_store: AIStore) -> None:
    if callback.message is None:
        await callback.answer()
        return

    charge = await ensure_balance(
        ai_store,
        callback,
        "currency_refresh",
        reply_markup=currency_keyboard(),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge

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
    else:
        await ai_store.charge_tokens(
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )

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
async def currency_menu_handler(callback: CallbackQuery, ai_store: AIStore) -> None:
    await render_currency(callback, ai_store)


@router.callback_query(F.data == "currency:refresh")
async def currency_refresh_handler(callback: CallbackQuery, ai_store: AIStore) -> None:
    await render_currency(callback, ai_store)


@router.message(Command("currency"))
async def currency_command_handler(message: Message, ai_store: AIStore) -> None:
    charge = await ensure_balance(
        ai_store,
        message,
        "currency_refresh",
        reply_markup=currency_keyboard(),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    progress_message = await send_wait_message(
        message,
        text="<b>Iltimos kuting...</b>\nValyuta kurslari olinmoqda.",
    )

    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
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
    else:
        await ai_store.charge_tokens(
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )
    if not await edit_wait_message(
        progress_message,
        text=text,
        reply_markup=currency_keyboard(),
    ):
        await clear_wait_message(progress_message)
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=currency_keyboard(),
        )
