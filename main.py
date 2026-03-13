import asyncio
import logging
import os
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import CallbackQuery, Message
from dotenv import load_dotenv

from handlers.admin import router as admin_router
from handlers.admin import is_admin_user_id
from handlers.ai_chat import router as ai_chat_router
from handlers.converter import router as converter_router
from handlers.currency import router as currency_router
from handlers.fallback import router as fallback_router
from handlers.jobs import router as jobs_router
from handlers.pollinations import router as pollinations_router
from handlers.premium import router as premium_router
from handlers.saver import router as saver_router
from handlers.shazam import router as shazam_router
from handlers.tempmail import router as tempmail_router
from handlers.tinyurl import router as tinyurl_router
from handlers.translate import router as translate_router
from handlers.weather import router as weather_router
from handlers.wikipedia import router as wikipedia_router
from handlers.youtube_search import router as youtube_search_router
from services.ai_store import AIContextMiddleware, AIStore
from services.analytics_store import AnalyticsMiddleware, AnalyticsStore
from services.group_command_mode import command_menu_text, install_group_command_mode, is_group_chat
from services.storage_config import (
    resolve_database_url,
    should_require_persistent_database,
)
from services.token_pricing import (
    free_reset_hours,
    free_reset_tokens,
    referral_invitee_bonus,
    referral_inviter_bonus,
)
from ui.main_menu import (
    main_menu_text,
    referral_keyboard,
    referral_menu_text,
    safe_edit_menu,
    section_menu_text,
    services_keyboard,
)

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
        raise ValueError("BOT_TOKEN topilmadiku. .env fayliga token yozing.")
    return token


def _mb_to_bytes(value: int) -> int:
    return max(1, value) * 1024 * 1024


def _normalize_bot_username(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.startswith("https://") or value.startswith("http://"):
        parsed = urlparse(value)
        tail = parsed.path.rsplit("/", 1)[-1].strip()
        value = tail
    elif "t.me/" in value:
        value = value.rsplit("/", 1)[-1].strip()
    value = value.split("?", 1)[0].strip().lstrip("@")
    return value


def register_core_handlers(
    dispatcher: Dispatcher,
    upload_limit_bytes: int,
    download_limit_bytes: int,
) -> None:
    def _referral_link(user_id: int) -> str:
        bot_username = _normalize_bot_username(os.getenv("BOT_USERNAME", ""))
        if bot_username:
            return f"https://t.me/{bot_username}?start=ref_{user_id}"
        return f"ref_{user_id}"

    def _start_payload(message: Message) -> str:
        text = str(message.text or "").strip()
        if not text:
            return ""
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) == 2 else ""

    def _referrer_id_from_payload(payload: str) -> int:
        clean = str(payload or "").strip().lower()
        if clean.startswith("ref_"):
            clean = clean[4:]
        try:
            return int(clean)
        except ValueError:
            return 0

    async def _menu_profile(ai_store: AIStore, user: object | None) -> dict[str, int | str]:
        if user is None:
            return {
                "user_plan": "free",
                "token_balance": 0,
                "referral_count": 0,
                "referral_link": "",
                "referrer_id": 0,
                "free_reset_date": "",
                "reset_date": "",
                "free_reset_tokens": free_reset_tokens(),
                "free_reset_hours": free_reset_hours(),
                "lifetime_tokens_earned": 0,
                "lifetime_tokens_spent": 0,
                "referral_inviter_bonus": referral_inviter_bonus(),
                "referral_invitee_bonus": referral_invitee_bonus(),
            }

        user_id = int(getattr(user, "id", 0) or 0)
        full_name = " ".join(
            part
            for part in [
                str(getattr(user, "first_name", "") or "").strip(),
                str(getattr(user, "last_name", "") or "").strip(),
            ]
            if part
        ).strip()
        profile = await ai_store.ensure_user(
            user_id=user_id,
            username=str(getattr(user, "username", "") or "").strip(),
            full_name=full_name,
        )
        return {
            "user_plan": str(profile.get("current_plan", "free") or "free"),
            "token_balance": int(profile.get("token_balance", 0) or 0),
            "referral_count": int(profile.get("referral_count", 0) or 0),
            "referral_link": _referral_link(user_id),
            "referrer_id": int(profile.get("referrer_id", 0) or 0),
            "free_reset_date": str(profile.get("free_reset_date", "") or ""),
            "reset_date": str(profile.get("reset_date", "") or ""),
            "free_reset_tokens": free_reset_tokens(),
            "free_reset_hours": free_reset_hours(),
            "lifetime_tokens_earned": int(profile.get("lifetime_tokens_earned", 0) or 0),
            "lifetime_tokens_spent": int(profile.get("lifetime_tokens_spent", 0) or 0),
            "referral_inviter_bonus": referral_inviter_bonus(),
            "referral_invitee_bonus": referral_invitee_bonus(),
        }

    async def _answer_main_menu(
        message: Message,
        *,
        ai_store: AIStore,
        notice: str = "",
    ) -> None:
        is_admin = is_admin_user_id(message.from_user.id if message.from_user else None)
        if is_group_chat(message):
            text = command_menu_text(is_admin=is_admin)
            if notice:
                text = f"{text}\n\n{notice}"
            await message.answer(text, parse_mode="HTML")
            return
        profile = await _menu_profile(ai_store, message.from_user)
        await message.answer(
            main_menu_text(
                upload_limit_bytes,
                download_limit_bytes,
                is_admin=is_admin,
                notice=notice,
                **profile,
            ),
            parse_mode="HTML",
            reply_markup=services_keyboard(
                is_admin=is_admin,
                referral_link=str(profile.get("referral_link", "") or ""),
            ),
        )

    async def _edit_main_menu(
        callback: CallbackQuery,
        *,
        ai_store: AIStore,
    ) -> None:
        if is_group_chat(callback):
            await callback.answer()
            if callback.message is not None:
                await callback.message.edit_text(
                    command_menu_text(
                        is_admin=is_admin_user_id(
                            callback.from_user.id if callback.from_user else None
                        )
                    ),
                    parse_mode="HTML",
                )
            return
        is_admin = is_admin_user_id(callback.from_user.id if callback.from_user else None)
        profile = await _menu_profile(ai_store, callback.from_user)
        await safe_edit_menu(
            callback,
            main_menu_text(
                upload_limit_bytes,
                download_limit_bytes,
                is_admin=is_admin,
                **profile,
            ),
            services_keyboard(
                is_admin=is_admin,
                referral_link=str(profile.get("referral_link", "") or ""),
            ),
        )

    async def _edit_section_menu(
        callback: CallbackQuery,
        *,
        section: str,
        ai_store: AIStore,
    ) -> None:
        is_admin = is_admin_user_id(callback.from_user.id if callback.from_user else None)
        profile = await _menu_profile(ai_store, callback.from_user)
        await safe_edit_menu(
            callback,
            section_menu_text(section, **profile),
            services_keyboard(
                is_admin=is_admin,
                section=section,
                referral_link=str(profile.get("referral_link", "") or ""),
            ),
        )

    async def _edit_referral_menu(
        callback: CallbackQuery,
        *,
        ai_store: AIStore,
    ) -> None:
        profile = await _menu_profile(ai_store, callback.from_user)
        await safe_edit_menu(
            callback,
            referral_menu_text(**profile),
            referral_keyboard(str(profile.get("referral_link", "") or "")),
        )

    @dispatcher.message(CommandStart())
    async def start_handler(
        message: Message,
        state: FSMContext,
        ai_store: AIStore,
    ) -> None:
        await state.clear()
        notice = ""
        referrer_id = _referrer_id_from_payload(_start_payload(message))
        if message.from_user is not None and referrer_id > 0:
            result = await ai_store.apply_referral(
                user_id=int(message.from_user.id),
                username=str(message.from_user.username or "").strip(),
                full_name=" ".join(
                    part
                    for part in [
                        str(message.from_user.first_name or "").strip(),
                        str(message.from_user.last_name or "").strip(),
                    ]
                    if part
                ).strip(),
                referrer_id=referrer_id,
            )
            if bool(result.get("applied")):
                notice = (
                    "🎁 Referal bonusi qo'shildi.\n"
                    f"Sizga: <b>{int(result.get('invitee_bonus', 0) or 0)}</b> token"
                )
        await _answer_main_menu(message, ai_store=ai_store, notice=notice)

    @dispatcher.message(Command("menu"))
    async def menu_handler(
        message: Message,
        state: FSMContext,
        ai_store: AIStore,
    ) -> None:
        await state.clear()
        await _answer_main_menu(message, ai_store=ai_store)

    @dispatcher.message(Command("help"))
    async def help_handler(message: Message) -> None:
        text = (
            "Buyruqlar:\n"
            "/start - asosiy menyu\n"
            "/menu - xizmatlar menyusi\n"
            "/ai - sun'iy intellekt bo'limi\n"
            "/image - rasm yaratish\n"
            "/youtube - YT / Insta / TikTok saver\n"
            "/save - direct fayl saqlash\n"
            "/weather - ob-havo\n"
            "/currency - valyuta\n"
            "/translate - tarjimon\n"
            "/jobs - ish qidirish\n"
            "/wiki - wikipedia\n"
            "/mail - temporary email\n"
            "/tinyurl - link qisqartirish\n"
            "/shazam - musiqa qidirish\n"
            "/convert - converter\n"
            "/premium - premium sahifasi\n"
            "/help - yordam\n"
            "/limits - saqlash limitlari\n\n"
            "Bir command yuboring va keyingi xabarlar shu servisga tegishli bo'ladi."
        )
        await message.answer(text)

    @dispatcher.message(Command("limits"))
    async def limits_handler(message: Message) -> None:
        text = (
            "<b>Saqlash limitlari</b>\n"
            f"Chatga qaytariladigan maksimal fayl: <b>{upload_limit_bytes // (1024 * 1024)} MB</b>\n"
            f"Botga yuboriladigan fayl limiti: <b>{download_limit_bytes // (1024 * 1024)} MB</b>"
        )
        await message.answer(text, parse_mode="HTML")

    @dispatcher.callback_query(F.data == "services:back")
    async def back_callback_handler(
        callback: CallbackQuery,
        state: FSMContext,
        ai_store: AIStore,
    ) -> None:
        await state.clear()
        await callback.answer()
        await _edit_main_menu(callback, ai_store=ai_store)

    @dispatcher.callback_query(F.data == "menu:main")
    async def menu_main_callback_handler(
        callback: CallbackQuery,
        state: FSMContext,
        ai_store: AIStore,
    ) -> None:
        await state.clear()
        await callback.answer()
        await _edit_main_menu(callback, ai_store=ai_store)

    @dispatcher.callback_query(F.data.startswith("menu:section:"))
    async def menu_section_callback_handler(
        callback: CallbackQuery,
        state: FSMContext,
        ai_store: AIStore,
    ) -> None:
        section = str(callback.data or "").rsplit(":", 1)[-1].strip().lower()
        if section not in {"ai", "media", "tools", "search", "cabinet"}:
            await callback.answer("Bo'lim topilmadi", show_alert=True)
            return
        await state.clear()
        await callback.answer()
        await _edit_section_menu(callback, section=section, ai_store=ai_store)

    @dispatcher.callback_query(F.data == "cabinet:referral")
    async def cabinet_referral_callback_handler(
        callback: CallbackQuery,
        state: FSMContext,
        ai_store: AIStore,
    ) -> None:
        await state.clear()
        await callback.answer()
        await _edit_referral_menu(callback, ai_store=ai_store)


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
    load_dotenv(override=False)
    install_group_command_mode()
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    main_logger = logging.getLogger("Main")

    database_url = resolve_database_url()
    if not database_url:
        if should_require_persistent_database():
            main_logger.error(
                "Persistent database topilmadi. Hosted deploy local JSON fallback bilan ishga tushirilmaydi. DATABASE_URL yoki Postgres envlarini sozlang."
            )
            return
        main_logger.warning(
            "DATABASE_URL yo'q. Bot local JSON storage bilan ishlaydi va redeploydan keyin data saqlanmaydi."
        )

    ai_log_channel_id = os.getenv("AI_LOG_CHANNEL_ID", "").strip()
    ai_log_channel_link = os.getenv("AI_LOG_CHANNEL_LINK", "").strip()
    if not ai_log_channel_id:
        main_logger.warning(
            "AI_LOG_CHANNEL_ID sozlanmagan. AI log kanal auto-detect holati local faylda turadi va redeploydan keyin yo'qolishi mumkin."
        )
        if ai_log_channel_link.startswith("https://t.me/+"):
            main_logger.warning(
                "AI_LOG_CHANNEL_LINK invite link ko'rinishida. Redeploydan keyin ishonchli AI logging uchun numeric AI_LOG_CHANNEL_ID kiriting."
            )

    try:
        bot_token = _read_bot_token()
    except ValueError as error:
        main_logger.error(str(error))
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
    try:
        me = await bot.get_me()
    except Exception as error:  # noqa: BLE001
        main_logger.warning("Bot username olinmadi: %s", error)
    else:
        resolved_username = _normalize_bot_username(getattr(me, "username", "") or "")
        env_username = _normalize_bot_username(os.getenv("BOT_USERNAME", ""))
        if resolved_username and resolved_username != env_username:
            os.environ["BOT_USERNAME"] = resolved_username
    dispatcher = Dispatcher(
        storage=MemoryStorage(),
        events_isolation=SimpleEventIsolation(),
    )
    analytics_store = AnalyticsStore(database_url=database_url)
    ai_store = AIStore(database_url=database_url)
    try:
        await analytics_store.startup()
        await ai_store.startup()
    except Exception as error:  # noqa: BLE001
        main_logger.error("Storage ishga tushmadi: %s", error)
        await bot.session.close()
        return
    analytics_middleware = AnalyticsMiddleware(analytics_store)
    ai_context_middleware = AIContextMiddleware(ai_store)
    dispatcher.message.outer_middleware(analytics_middleware)
    dispatcher.callback_query.outer_middleware(analytics_middleware)
    dispatcher.message.outer_middleware(ai_context_middleware)
    dispatcher.callback_query.outer_middleware(ai_context_middleware)
    register_core_handlers(
        dispatcher,
        _mb_to_bytes(upload_limit_mb),
        _mb_to_bytes(download_limit_mb),
    )

    dispatcher.include_router(admin_router)
    dispatcher.include_router(premium_router)
    dispatcher.include_router(ai_chat_router)
    dispatcher.include_router(saver_router)
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
    dispatcher.include_router(pollinations_router)
    dispatcher.include_router(fallback_router)

    try:
        await run_polling_forever(dispatcher, bot, polling_restart_delay_seconds)
    finally:
        await analytics_store.shutdown()
        await ai_store.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
