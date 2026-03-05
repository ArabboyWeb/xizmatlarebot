import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
import aiohttp

from services.saver import (
    DirectLinkDownloaderBot,
    InstanceLock,
    build_bot_session,
    format_bytes,
    load_config,
    run_polling_forever,
    setup_logging,
    verify_bot_access,
)
from handlers.converter import router as converter_router
from handlers.currency import router as currency_router
from handlers.pollinations import router as pollinations_router
from handlers.rembg import router as rembg_router
from handlers.shazam import router as shazam_router
from handlers.tempmail import router as tempmail_router
from handlers.tinyurl import router as tinyurl_router
from handlers.translate import router as translate_router
from handlers.weather import router as weather_router
from handlers.wikipedia import router as wikipedia_router
from ui.main_menu import (
    main_menu_text,
    safe_edit_menu,
    save_keyboard,
    save_menu_text,
    services_keyboard,
)


def register_core_handlers(
    dispatcher: Dispatcher, app: DirectLinkDownloaderBot, max_file_bytes: int
) -> None:
    @dispatcher.message(CommandStart())
    async def start_handler(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            main_menu_text(max_file_bytes),
            parse_mode="HTML",
            reply_markup=services_keyboard(),
        )

    @dispatcher.message(Command("menu"))
    async def menu_handler(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            main_menu_text(max_file_bytes),
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
            "/limits - joriy limitlar\n"
            "/process - downloader holati\n"
            "/stats - umumiy statistika\n"
            "/cancel - joriy yuklashni bekor qilish\n\n"
            "Link formatlari: direct HTTP/HTTPS yoki YouTube."
        )
        await message.answer(text)

    @dispatcher.message(Command("limits"))
    async def limits_handler(message: Message) -> None:
        config = app.config
        allowed_mode = (
            "Hamma foydalanuvchi"
            if not config.allowed_user_ids
            else "Faqat ruxsat berilgan ID lar"
        )
        speed_mode = "dual-worker" if config.download_workers >= 2 else "single-worker"
        text = (
            f"Fayl limiti: {format_bytes(config.max_file_bytes)}\n"
            f"Global parallel yuklash: {config.concurrent_downloads}\n"
            f"Per-user limit: {config.per_user_download_limit}\n"
            f"Download workers: {config.download_workers} ({speed_mode})\n"
            f"Parallel min size: {format_bytes(config.parallel_download_min_bytes)}\n"
            f"Connector limit: {config.http_connector_limit}/{config.http_connector_limit_per_host}\n"
            f"Upload chunk: {config.upload_chunk_kb} KB\n"
            f"Send progress interval: {config.send_progress_interval_seconds:.1f}s\n"
            f"YouTube: {'on' if config.youtube_enabled else 'off'}\n"
            f"YouTube timeout: {config.youtube_timeout_seconds}s\n"
            "Send mode: auto media + fallback document\n"
            f"Retry: {config.max_retries} marta\n"
            f"Kirish rejimi: {allowed_mode}"
        )
        await message.answer(text)

    @dispatcher.message(Command("process"))
    async def process_handler(message: Message) -> None:
        await message.answer(app.build_process_snapshot())

    @dispatcher.message(Command("stats"))
    async def stats_handler(message: Message) -> None:
        text = (
            f"Tugallangan: {app.completed_downloads}\n"
            f"Xatoliklar: {app.failed_downloads}\n"
            f"Jami yuklangan: {format_bytes(app.total_downloaded_bytes)}\n"
            f"Active users: {len(app.active_per_user)}"
        )
        await message.answer(text)

    @dispatcher.message(Command("cancel"))
    async def cancel_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        cancelled = await app.cancel_user_task(user_id)
        if cancelled:
            await message.answer("Joriy yuklash bekor qilindi.")
        else:
            await message.answer("Bekor qilish uchun faol yuklash topilmadi.")

    @dispatcher.callback_query(F.data == "services:save")
    async def save_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        await safe_edit_menu(callback, save_menu_text(max_file_bytes), save_keyboard())

    @dispatcher.callback_query(F.data == "services:back")
    async def back_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        await safe_edit_menu(
            callback, main_menu_text(max_file_bytes), services_keyboard()
        )

    @dispatcher.message(F.text | F.caption)
    async def downloader_handler(message: Message, state: FSMContext) -> None:
        if message.text and message.text.lstrip().startswith("/"):
            return
        if await state.get_state():
            return
        await app.handle_link(message)


async def main() -> None:
    try:
        config = load_config()
    except ValueError as error:
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
        logging.getLogger("Main").error(str(error))
        return

    setup_logging(config)
    logger = logging.getLogger("Main")
    lock = InstanceLock(config.lock_file)
    try:
        lock.acquire()
    except Exception as error:  # noqa: BLE001
        logger.error(str(error))
        return

    logger.info("Xizmatlar e-bot ishga tushmoqda")
    logger.info("Temp dir: %s", config.temp_dir.resolve())
    logger.info("Max size: %s", format_bytes(config.max_file_bytes))
    logger.info("Lock file: %s", config.lock_file.resolve())

    app = DirectLinkDownloaderBot(config)
    await app.startup()

    bot_session = build_bot_session(config)
    bot = Bot(token=config.bot_token, session=bot_session)
    dispatcher = Dispatcher(storage=MemoryStorage())
    register_core_handlers(dispatcher, app, config.max_file_bytes)
    dispatcher.include_router(weather_router)
    dispatcher.include_router(currency_router)
    dispatcher.include_router(converter_router)
    dispatcher.include_router(tempmail_router)
    dispatcher.include_router(tinyurl_router)
    dispatcher.include_router(shazam_router)
    dispatcher.include_router(translate_router)
    dispatcher.include_router(wikipedia_router)
    dispatcher.include_router(rembg_router)
    dispatcher.include_router(pollinations_router)

    try:
        await verify_bot_access(bot)
        await run_polling_forever(dispatcher, bot, config)
    except ValueError as error:
        logger.critical(str(error))
    except (TelegramNetworkError, aiohttp.ClientError, asyncio.TimeoutError) as error:
        logger.exception("Pollingda tarmoq xatosi: %s", error)
    finally:
        await app.shutdown()
        await bot.session.close()
        lock.release()


if __name__ == "__main__":
    asyncio.run(main())
