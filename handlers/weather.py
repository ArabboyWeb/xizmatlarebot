import html
import logging

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
from aiogram.utils.chat_action import ChatActionSender

from services.ai_store import AIStore
from services.group_command_mode import is_group_chat
from services.token_billing import ensure_balance
from services.weather_client import (
    build_weather_html,
    fetch_current_weather,
    resolve_city,
    reverse_location_name,
)

router = Router(name="weather")
logger = logging.getLogger(__name__)


class WeatherState(StatesGroup):
    waiting_city = State()
    waiting_location = State()


def weather_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Shahar nomi", callback_data="weather:city")],
            [
                InlineKeyboardButton(
                    text="Lokatsiya yuborish", callback_data="weather:location"
                )
            ],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


def weather_city_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Boshqa shahar", callback_data="weather:city")],
            [
                InlineKeyboardButton(
                    text="Lokatsiya bo'yicha", callback_data="weather:location"
                )
            ],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


def weather_location_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Boshqa lokatsiya", callback_data="weather:location"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Shahar bo'yicha", callback_data="weather:city"
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


async def _send_weather_by_city(message: Message, city: str) -> None:
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        city_data = await resolve_city(city)
        current = await fetch_current_weather(
            city_data["latitude"], city_data["longitude"]
        )

    city_name = str(city_data.get("name", city))
    country = str(city_data.get("country", ""))
    location_name = f"{city_name}, {country}".strip(", ")

    await message.answer(
        build_weather_html(location_name, current),
        parse_mode="HTML",
        reply_markup=weather_city_keyboard(),
    )


async def _send_weather_by_location(
    message: Message, latitude: float, longitude: float
) -> None:
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        current = await fetch_current_weather(latitude, longitude)
        location_name = await reverse_location_name(latitude, longitude)

    await message.answer(
        build_weather_html(location_name, current),
        parse_mode="HTML",
        reply_markup=weather_location_keyboard(),
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
            logger.warning("Weather edit xatosi: %s", error)


@router.callback_query(F.data == "services:weather")
async def weather_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _safe_edit(
        callback,
        "<b>Ob-havo bo'limi (Open-Meteo)</b>\nKerakli usulni tanlang:",
        weather_menu_keyboard(),
    )


@router.message(Command("weather"))
async def weather_command_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    if is_group_chat(message):
        await state.set_state(WeatherState.waiting_city)
        await message.answer(
            "<b>Ob-havo</b>\nShahar nomini yozing yoki lokatsiya yuboring.",
            parse_mode="HTML",
            reply_markup=back_keyboard(),
        )
        return
    await message.answer(
        "<b>Ob-havo bo'limi (Open-Meteo)</b>\nKerakli usulni tanlang:",
        parse_mode="HTML",
        reply_markup=weather_menu_keyboard(),
    )


@router.callback_query(F.data == "weather:city")
async def weather_city_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WeatherState.waiting_city)
    await callback.answer()
    await _safe_edit(
        callback,
        "<b>Shahar nomini yuboring</b>\nMasalan: <code>Tashkent</code>",
        back_keyboard(),
    )


@router.callback_query(F.data == "weather:location")
async def weather_location_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WeatherState.waiting_location)
    await callback.answer()
    await _safe_edit(
        callback,
        "<b>Lokatsiyangizni yuboring</b>\nAttachment menyusidan location yuboring.",
        back_keyboard(),
    )


@router.message(WeatherState.waiting_city, F.text & ~F.text.startswith("/"))
async def weather_city_message(message: Message, ai_store: AIStore) -> None:
    city = (message.text or "").strip()
    if not city or city.startswith("/"):
        return
    charge = await ensure_balance(
        ai_store,
        message,
        "weather_lookup",
        reply_markup=weather_city_keyboard(),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    try:
        await _send_weather_by_city(message, city)
    except (ValueError, RuntimeError, TimeoutError, ConnectionError) as error:
        await message.answer(
            f"<b>Ob-havo xatosi</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=weather_city_keyboard(),
        )
    except Exception as error:  # noqa: BLE001
        logger.exception("Weather modulida kutilmagan xatolik")
        await message.answer(
            f"<b>Kutilmagan xatolik</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=weather_city_keyboard(),
        )
    else:
        await ai_store.charge_tokens(
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )


@router.message(WeatherState.waiting_city, F.location)
async def weather_city_location_message(message: Message, ai_store: AIStore) -> None:
    if message.location is None:
        return
    charge = await ensure_balance(
        ai_store,
        message,
        "weather_lookup",
        reply_markup=weather_location_keyboard(),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    try:
        await _send_weather_by_location(
            message, message.location.latitude, message.location.longitude
        )
    except (ValueError, RuntimeError, TimeoutError, ConnectionError) as error:
        await message.answer(
            f"<b>Ob-havo xatosi</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=weather_location_keyboard(),
        )
    except Exception as error:  # noqa: BLE001
        logger.exception("Weather modulida kutilmagan xatolik")
        await message.answer(
            f"<b>Kutilmagan xatolik</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=weather_location_keyboard(),
        )
    else:
        await ai_store.charge_tokens(
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )


@router.message(WeatherState.waiting_location, F.location)
async def weather_location_message(message: Message, ai_store: AIStore) -> None:
    if message.location is None:
        return
    charge = await ensure_balance(
        ai_store,
        message,
        "weather_lookup",
        reply_markup=weather_location_keyboard(),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    try:
        await _send_weather_by_location(
            message, message.location.latitude, message.location.longitude
        )
    except (ValueError, RuntimeError, TimeoutError, ConnectionError) as error:
        await message.answer(
            f"<b>Ob-havo xatosi</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=weather_location_keyboard(),
        )
    except Exception as error:  # noqa: BLE001
        logger.exception("Weather modulida kutilmagan xatolik")
        await message.answer(
            f"<b>Kutilmagan xatolik</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=weather_location_keyboard(),
        )
    else:
        await ai_store.charge_tokens(
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=cost,
        )


@router.message(WeatherState.waiting_location, F.text & ~F.text.startswith("/"))
async def weather_location_text_fallback(message: Message) -> None:
    await message.answer(
        "Lokatsiya yuborish uchun Telegram attachment menyusidan <b>Location</b> ni tanlang.",
        parse_mode="HTML",
        reply_markup=back_keyboard(),
    )
