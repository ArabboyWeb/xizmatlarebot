import asyncio
import logging
import os

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from dotenv import load_dotenv

from handlers.converter import router as converter_router
from handlers.currency import router as currency_router
from handlers.fallback import router as fallback_router
from handlers.jobs import router as jobs_router
from handlers.pollinations import router as pollinations_router
from handlers.rembg import router as rembg_router
from handlers.shazam import router as shazam_router
from handlers.tempmail import router as tempmail_router
from handlers.tinyurl import router as tinyurl_router
from handlers.translate import router as translate_router
from handlers.weather import router as weather_router
from handlers.wikipedia import router as wikipedia_router
from handlers.youtube_search import router as youtube_search_router
from ui.main_menu import main_menu_text, safe_edit_menu, services_keyboard

DEFAULT_POLLING_RESTART_DELAY_SECONDS = 8
DEFAULT_TELEGRAM_UPLOAD_LIMIT_MB = 50
DEFAULT_TELEGRAM_DOWNLOAD_LIMIT_MB = 20


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _read_bot_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("BOT_TOKEN topilmadi. .env fayliga token yozing.")
    return token


def _mb_to_bytes(value: int) -> int:
    return max(1, value) * 1024 * 1024


def register_core_handlers(
    dispatcher: Dispatcher,
    upload_limit_bytes: int,
    download_limit_bytes: int,
) -> None:
    @dispatcher.message(CommandStart())
    async def start_handler(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            main_menu_text(upload_limit_bytes, download_limit_bytes),
            parse_mode="HTML",
            reply_markup=services_keyboard(),
        )

    @dispatcher.message(Command("menu"))
    async def menu_handler(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            main_menu_text(upload_limit_bytes, download_limit_bytes),
            parse_mode="HTML",
            reply_markup=services_keyboard(),
        )

    @dispatcher.message(Command("help"))
    async def help_handler(message: Message) -> None:
        text = (
            "Buyruqlar:\n"
            "/start - asosiy menyu\n"
            "/menu - xizmatlar menyusi\n"
            "/help - yordam\n"
            "/limits - Telegram free limitlar\n\n"
            "Botda faqat free servislar ishlatiladi."
        )
        await message.answer(text)

    @dispatcher.message(Command("limits"))
    async def limits_handler(message: Message) -> None:
        text = (
            "<b>Telegram Free Cloud Limitlari</b>\n"
            f"Bot upload limiti: <b>{upload_limit_bytes // (1024 * 1024)} MB</b>\n"
            f"Bot download (getFile) limiti: <b>{download_limit_bytes // (1024 * 1024)} MB</b>\n\n"
            "Eslatma: bu qiymatlar cloud Bot API uchun aniqlik maqsadida ko'rsatilgan."
        )
        await message.answer(text, parse_mode="HTML")

    @dispatcher.callback_query(F.data == "services:back")
    async def back_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        await safe_edit_menu(
            callback,
            main_menu_text(upload_limit_bytes, download_limit_bytes),
            services_keyboard(),
        )


async def run_polling_forever(
    dispatcher: Dispatcher, bot: Bot, restart_delay_seconds: int
) -> None:
    logger = logging.getLogger("Polling")
    while True:
        try:
            await dispatcher.start_polling(
                bot, allowed_updates=dispatcher.resolve_used_update_types()
            )
            logger.info("Polling normal to'xtadi.")
            break
        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            logger.info("Polling to'xtatildi.")
            break
        except (TelegramNetworkError, aiohttp.ClientError, asyncio.TimeoutError) as error:
            logger.exception("Polling tarmoq xatosi: %s", error)
            await asyncio.sleep(restart_delay_seconds)
        except Exception as error:  # noqa: BLE001
            logger.exception("Kutilmagan polling xatosi: %s", error)
            await asyncio.sleep(restart_delay_seconds)


async def main() -> None:
    load_dotenv(override=True)
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    try:
        bot_token = _read_bot_token()
    except ValueError as error:
        logging.getLogger("Main").error(str(error))
        return

    upload_limit_mb = _read_int(
        "TELEGRAM_FREE_UPLOAD_LIMIT_MB", DEFAULT_TELEGRAM_UPLOAD_LIMIT_MB
    )
    download_limit_mb = _read_int(
        "TELEGRAM_FREE_DOWNLOAD_LIMIT_MB", DEFAULT_TELEGRAM_DOWNLOAD_LIMIT_MB
    )
    polling_restart_delay_seconds = max(
        3,
        _read_int(
            "POLLING_RESTART_DELAY_SECONDS", DEFAULT_POLLING_RESTART_DELAY_SECONDS
        ),
    )

    bot = Bot(token=bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    register_core_handlers(
        dispatcher,
        _mb_to_bytes(upload_limit_mb),
        _mb_to_bytes(download_limit_mb),
    )

    dispatcher.include_router(weather_router)
    dispatcher.include_router(currency_router)
    dispatcher.include_router(converter_router)
    dispatcher.include_router(tempmail_router)
    dispatcher.include_router(tinyurl_router)
    dispatcher.include_router(shazam_router)
    dispatcher.include_router(translate_router)
    dispatcher.include_router(jobs_router)
    dispatcher.include_router(youtube_search_router)
    dispatcher.include_router(wikipedia_router)
    dispatcher.include_router(rembg_router)
    dispatcher.include_router(pollinations_router)
    dispatcher.include_router(fallback_router)

    try:
        await run_polling_forever(dispatcher, bot, polling_restart_delay_seconds)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
