from __future__ import annotations

import html
import logging
import random

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.types.input_file import BufferedInputFile
from aiogram.utils.chat_action import ChatActionSender

from services.ai_channel_logger import log_image_generation
from services.ai_costs import estimate_imagen_cost_usd
from services.ai_store import AIStore
from services.imagen_client import generate_imagen_image
from services.pollinations_client import generate_image
from services.token_pricing import (
    free_ai_image_cooldown_seconds,
    free_ai_image_limit_per_day,
    premium_ai_image_credit_cost,
    premium_ai_image_cooldown_seconds,
)

router = Router(name="pollinations")
logger = logging.getLogger(__name__)

FREE_MODELS = ("flux", "turbo")
SIZES: tuple[tuple[int, int], ...] = ((1024, 1024), (1024, 768), (768, 1024))


class PollinationsState(StatesGroup):
    waiting_prompt = State()


def _model_label(model: str, active_model: str) -> str:
    base = model.upper()
    return f"[{base}]" if model == active_model else base


def _size_label(width: int, height: int, active_size: tuple[int, int]) -> str:
    base = f"{width}x{height}"
    return f"[{base}]" if (width, height) == active_size else base


def pollinations_keyboard(
    active_model: str,
    active_size: tuple[int, int],
    *,
    is_premium: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if not is_premium:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_model_label("flux", active_model),
                    callback_data="pollinations:model:flux",
                ),
                InlineKeyboardButton(
                    text=_model_label("turbo", active_model),
                    callback_data="pollinations:model:turbo",
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=_size_label(1024, 1024, active_size),
                callback_data="pollinations:size:1024:1024",
            ),
            InlineKeyboardButton(
                text=_size_label(1024, 768, active_size),
                callback_data="pollinations:size:1024:768",
            ),
            InlineKeyboardButton(
                text=_size_label(768, 1024, active_size),
                callback_data="pollinations:size:768:1024",
            ),
        ]
    )
    rows.append([InlineKeyboardButton(text="Orqaga", callback_data="services:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pollinations_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yana yaratish", callback_data="pollinations:repeat")],
            [InlineKeyboardButton(text="Sozlamalar", callback_data="pollinations:menu")],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


def _settings(data: dict[str, object]) -> tuple[str, tuple[int, int]]:
    model = str(data.get("pollinations_model", "flux")).lower()
    if model not in FREE_MODELS:
        model = "flux"
    width = int(data.get("pollinations_width", 1024))
    height = int(data.get("pollinations_height", 1024))
    if (width, height) not in SIZES:
        width, height = 1024, 1024
    return model, (width, height)


def _prompt_text(*, plan: str, model: str, size: tuple[int, int]) -> str:
    if plan == "premium":
        return (
            "<b>Rasm yaratish</b>\n"
            "Model: <b>Imagen 4 Fast</b>\n"
            f"O'lcham: <b>{size[0]}x{size[1]}</b>\n"
            f"Narx: <b>{premium_ai_image_credit_cost()}</b> kredit / rasm\n\n"
            "Prompt yuboring, bot premium rasm yaratib qaytaradi."
        )
    return (
        "<b>Rasm yaratish</b>\n"
        f"Model: <b>{html.escape(model.upper())}</b>\n"
        f"O'lcham: <b>{size[0]}x{size[1]}</b>\n"
        f"Free limit: <b>{free_ai_image_limit_per_day()} rasm / kun</b>\n"
        f"Kutish: <b>{free_ai_image_cooldown_seconds() // 60} daqiqa</b>\n\n"
        "Prompt yuboring, bot rasm yaratib qaytaradi."
    )


def _public_generation_error() -> str:
    return "Rasmni yaratib bo'lmadi. Promptni o'zgartirib yoki qisqartirib qayta urinib ko'ring."


async def _safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in (error.message or "").lower():
            logger.warning("Pollinations edit xatosi: %s", error)


async def _show_menu(callback: CallbackQuery, state: FSMContext, ai_store: AIStore) -> None:
    model, size = _settings(await state.get_data())
    user_id = int(callback.from_user.id if callback.from_user else 0)
    username = str(getattr(callback.from_user, "username", "") or "").strip()
    full_name = " ".join(
        part
        for part in [
            str(getattr(callback.from_user, "first_name", "") or "").strip(),
            str(getattr(callback.from_user, "last_name", "") or "").strip(),
        ]
        if part
    ).strip()
    user = await ai_store.ensure_user(
        user_id=user_id,
        username=username,
        full_name=full_name,
    )
    current_plan = str(user.get("current_plan", "free") or "free")
    await state.set_state(PollinationsState.waiting_prompt)
    await _safe_edit(
        callback,
        _prompt_text(plan=current_plan, model=model, size=size),
        pollinations_keyboard(model, size, is_premium=current_plan == "premium"),
    )


@router.callback_query(F.data == "services:pollinations")
async def pollinations_entry_handler(
    callback: CallbackQuery,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    await state.clear()
    await state.update_data(
        pollinations_model="flux",
        pollinations_width=1024,
        pollinations_height=1024,
    )
    await callback.answer()
    await _show_menu(callback, state, ai_store)


@router.callback_query(F.data == "pollinations:menu")
async def pollinations_menu_handler(
    callback: CallbackQuery,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    await callback.answer()
    await _show_menu(callback, state, ai_store)


@router.callback_query(F.data == "pollinations:repeat")
async def pollinations_repeat_handler(
    callback: CallbackQuery,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    await callback.answer()
    await _show_menu(callback, state, ai_store)


@router.message(Command("image"))
@router.message(Command("art"))
async def pollinations_command_handler(
    message: Message,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    await state.clear()
    await state.update_data(
        pollinations_model="flux",
        pollinations_width=1024,
        pollinations_height=1024,
    )
    await state.set_state(PollinationsState.waiting_prompt)
    user_id = int(message.from_user.id if message.from_user else 0)
    username = str(getattr(message.from_user, "username", "") or "").strip()
    full_name = " ".join(
        part
        for part in [
            str(getattr(message.from_user, "first_name", "") or "").strip(),
            str(getattr(message.from_user, "last_name", "") or "").strip(),
        ]
        if part
    ).strip()
    user = await ai_store.ensure_user(
        user_id=user_id,
        username=username,
        full_name=full_name,
    )
    current_plan = str(user.get("current_plan", "free") or "free")
    await message.answer(
        _prompt_text(plan=current_plan, model="flux", size=(1024, 1024)),
        parse_mode="HTML",
        reply_markup=pollinations_keyboard("flux", (1024, 1024), is_premium=current_plan == "premium"),
    )


@router.callback_query(F.data.startswith("pollinations:model:"))
async def pollinations_model_handler(
    callback: CallbackQuery,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    raw = callback.data or ""
    parts = raw.split(":")
    if len(parts) != 3:
        await callback.answer("Model noto'g'ri", show_alert=True)
        return
    model = parts[2].lower()
    if model not in FREE_MODELS:
        await callback.answer("Model topilmadi", show_alert=True)
        return
    await state.update_data(pollinations_model=model)
    await callback.answer("Model saqlandi")
    await _show_menu(callback, state, ai_store)


@router.callback_query(F.data.startswith("pollinations:size:"))
async def pollinations_size_handler(
    callback: CallbackQuery,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    raw = callback.data or ""
    parts = raw.split(":")
    if len(parts) != 4:
        await callback.answer("O'lcham noto'g'ri", show_alert=True)
        return
    try:
        width = int(parts[2])
        height = int(parts[3])
    except ValueError:
        await callback.answer("O'lcham noto'g'ri", show_alert=True)
        return
    if (width, height) not in SIZES:
        await callback.answer("O'lcham topilmadi", show_alert=True)
        return
    await state.update_data(pollinations_width=width, pollinations_height=height)
    await callback.answer("O'lcham saqlandi")
    await _show_menu(callback, state, ai_store)


@router.message(PollinationsState.waiting_prompt, F.text & ~F.text.startswith("/"))
async def pollinations_prompt_handler(
    message: Message,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    prompt = (message.text or "").strip()
    if not prompt or prompt.startswith("/"):
        return
    if message.from_user is None:
        return

    model, size = _settings(await state.get_data())
    user_id = int(message.from_user.id)
    username = str(message.from_user.username or "").strip()
    full_name = " ".join(
        part
        for part in [
            str(message.from_user.first_name or "").strip(),
            str(message.from_user.last_name or "").strip(),
        ]
        if part
    ).strip()
    user = await ai_store.ensure_user(
        user_id=user_id,
        username=username,
        full_name=full_name,
    )
    current_plan = str(user.get("current_plan", "free") or "free").strip().lower()
    authorization = await ai_store.authorize_ai_service(
        user_id=user_id,
        username=username,
        full_name=full_name,
        service_key="pollinations_generate",
        credit_cost=premium_ai_image_credit_cost(),
        estimated_ai_cost_usd=estimate_imagen_cost_usd(image_count=1) if current_plan == "premium" else 0.0,
        cooldown_seconds=(
            premium_ai_image_cooldown_seconds()
            if current_plan == "premium"
            else free_ai_image_cooldown_seconds()
        ),
        free_daily_limit=free_ai_image_limit_per_day(),
    )
    if not bool(authorization.get("ok")):
        reason = str(authorization.get("reason", "") or "")
        keyboard = pollinations_keyboard(model, size, is_premium=current_plan == "premium")
        if reason == "cooldown":
            await message.answer(
                (
                    "<b>Kutish kerak</b>\n"
                    f"Keyingi rasm uchun <b>{int(authorization.get('wait_seconds', 0) or 0)}</b> soniya kuting."
                ),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        if reason == "daily_limit":
            await message.answer(
                (
                    "<b>Kunlik free rasm limiti tugadi</b>\n"
                    f"Bugungi limit: <b>{free_ai_image_limit_per_day()}</b> ta rasm."
                ),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        if reason == "insufficient_credits":
            await message.answer(
                (
                    "<b>Kredit yetarli emas</b>\n"
                    f"Premium rasm uchun <b>{premium_ai_image_credit_cost()}</b> kredit kerak."
                ),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        if reason == "budget_cap":
            await message.answer(
                "<b>AI byudjet limiti tugadi</b>",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        await message.answer(
            "<b>Rasm so'rovi hozir qabul qilinmadi</b>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    hold_id = str(authorization.get("hold_id", "") or "")
    seed = random.randint(1000, 9_999_999)
    provider_model = model
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            if current_plan == "premium":
                image = await generate_imagen_image(
                    prompt,
                    width=size[0],
                    height=size[1],
                )
                provider_model = "imagen-4.0-fast-generate-001"
            else:
                image = await generate_image(
                    prompt,
                    model=model,
                    width=size[0],
                    height=size[1],
                    seed=seed,
                )
                provider_model = model
    except Exception as error:  # noqa: BLE001
        logger.warning("Rasm yaratish xatosi: %s", error)
        await ai_store.finalize_ai_service(
            user_id=user_id,
            username=username,
            full_name=full_name,
            service_key="pollinations_generate",
            ok=False,
            hold_id=hold_id,
            note=str(error),
        )
        await message.answer(
            f"<b>Rasm yaratish xatosi</b>\n{_public_generation_error()}",
            parse_mode="HTML",
            reply_markup=pollinations_keyboard(model, size, is_premium=current_plan == "premium"),
        )
        return

    file_name = (
        f"imagen_4_fast_{size[0]}x{size[1]}_{seed}.png"
        if current_plan == "premium"
        else f"rasm_{model}_{size[0]}x{size[1]}_{seed}.png"
    )
    photo = BufferedInputFile(image, filename=file_name)
    caption = (
        "<b>Rasm tayyor</b>\n"
        f"Model: <b>{html.escape('Imagen 4 Fast' if current_plan == 'premium' else model.upper())}</b>\n"
        f"O'lcham: <b>{size[0]}x{size[1]}</b>\n"
        f"Prompt: <code>{html.escape(prompt[:180])}</code>"
    )
    try:
        await message.answer_photo(
            photo=photo,
            caption=caption,
            parse_mode="HTML",
            reply_markup=pollinations_result_keyboard(),
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("Rasm photo yuborilmadi, document fallback ishladi: %s", error)
        document = BufferedInputFile(image, filename=file_name)
        await message.answer_document(
            document=document,
            caption=caption,
            parse_mode="HTML",
            reply_markup=pollinations_result_keyboard(),
        )

    updated_user = await ai_store.finalize_ai_service(
        user_id=user_id,
        username=username,
        full_name=full_name,
        service_key="pollinations_generate",
        ok=True,
        hold_id=hold_id,
        actual_ai_cost_usd=estimate_imagen_cost_usd(image_count=1) if current_plan == "premium" else 0.0,
        note=provider_model,
    )
    try:
        await log_image_generation(
            message.bot,
            user_id=user_id,
            username=username,
            full_name=full_name,
            prompt_text=prompt,
            model=provider_model,
            width=size[0],
            height=size[1],
            seed=seed,
            image_bytes=image,
            file_name=file_name,
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("AI image log kanalga yuborilmadi: %s", error)
    if current_plan == "premium":
        await message.answer(
            f"Qolgan kredit: <b>{int(updated_user.get('credit_balance', updated_user.get('token_balance', 0)) or 0)}</b>",
            parse_mode="HTML",
        )
    await state.set_state(PollinationsState.waiting_prompt)


@router.message(PollinationsState.waiting_prompt)
async def pollinations_fallback(message: Message, state: FSMContext, ai_store: AIStore) -> None:
    model, size = _settings(await state.get_data())
    user_id = int(message.from_user.id if message.from_user else 0)
    username = str(getattr(message.from_user, "username", "") or "").strip()
    full_name = " ".join(
        part
        for part in [
            str(getattr(message.from_user, "first_name", "") or "").strip(),
            str(getattr(message.from_user, "last_name", "") or "").strip(),
        ]
        if part
    ).strip()
    user = await ai_store.ensure_user(
        user_id=user_id,
        username=username,
        full_name=full_name,
    )
    await message.answer(
        "Prompt sifatida oddiy matn yuboring.",
        reply_markup=pollinations_keyboard(
            model,
            size,
            is_premium=str(user.get("current_plan", "free") or "free") == "premium",
        ),
    )
