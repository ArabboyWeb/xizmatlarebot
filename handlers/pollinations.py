import html
import logging
import random

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
from aiogram.types.input_file import BufferedInputFile
from aiogram.utils.chat_action import ChatActionSender

from services.ai_store import AIStore
from services.pollinations_client import generate_image
from services.token_billing import ensure_balance

router = Router(name="pollinations")
logger = logging.getLogger(__name__)

MODELS = ("flux", "turbo")
SIZES: tuple[tuple[int, int], ...] = ((1024, 1024), (1024, 768), (768, 1024))


class PollinationsState(StatesGroup):
    waiting_prompt = State()


def _model_label(model: str, active_model: str) -> str:
    base = model.upper()
    return f"{'[' if model == active_model else ''}{base}{']' if model == active_model else ''}"


def _size_label(width: int, height: int, active_size: tuple[int, int]) -> str:
    base = f"{width}x{height}"
    return f"{'[' if (width, height) == active_size else ''}{base}{']' if (width, height) == active_size else ''}"


def pollinations_keyboard(
    active_model: str, active_size: tuple[int, int]
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_model_label("flux", active_model),
                    callback_data="pollinations:model:flux",
                ),
                InlineKeyboardButton(
                    text=_model_label("turbo", active_model),
                    callback_data="pollinations:model:turbo",
                ),
            ],
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
            ],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


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
    if model not in MODELS:
        model = "flux"

    width = int(data.get("pollinations_width", 1024))
    height = int(data.get("pollinations_height", 1024))
    if (width, height) not in SIZES:
        width, height = 1024, 1024
    return model, (width, height)


def _prompt_text(model: str, size: tuple[int, int]) -> str:
    return (
        "<b>Rasm yaratish</b>\n"
        f"Model: <b>{html.escape(model.upper())}</b>\n"
        f"O'lcham: <b>{size[0]}x{size[1]}</b>\n\n"
        "Prompt yuboring, bot rasm yaratib qaytaradi."
    )


def _public_generation_error() -> str:
    return "Rasmni yaratib bo'lmadi. Promptni o'zgartirib yoki qisqartirib qayta urinib ko'ring."


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
            logger.warning("Pollinations edit xatosi: %s", error)


async def _show_menu(callback: CallbackQuery, state: FSMContext) -> None:
    model, size = _settings(await state.get_data())
    await state.set_state(PollinationsState.waiting_prompt)
    await _safe_edit(
        callback,
        _prompt_text(model, size),
        pollinations_keyboard(model, size),
    )


@router.callback_query(F.data == "services:pollinations")
async def pollinations_entry_handler(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await state.clear()
    await state.update_data(
        pollinations_model="flux",
        pollinations_width=1024,
        pollinations_height=1024,
    )
    await callback.answer()
    await _show_menu(callback, state)


@router.callback_query(F.data == "pollinations:menu")
async def pollinations_menu_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_menu(callback, state)


@router.callback_query(F.data == "pollinations:repeat")
async def pollinations_repeat_handler(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await callback.answer()
    await _show_menu(callback, state)


@router.message(Command("image"))
@router.message(Command("art"))
async def pollinations_command_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(
        pollinations_model="flux",
        pollinations_width=1024,
        pollinations_height=1024,
    )
    await state.set_state(PollinationsState.waiting_prompt)
    await message.answer(
        _prompt_text("flux", (1024, 1024)),
        parse_mode="HTML",
        reply_markup=pollinations_keyboard("flux", (1024, 1024)),
    )


@router.callback_query(F.data.startswith("pollinations:model:"))
async def pollinations_model_handler(
    callback: CallbackQuery, state: FSMContext
) -> None:
    raw = callback.data or ""
    parts = raw.split(":")
    if len(parts) != 3:
        await callback.answer("Model noto'g'ri", show_alert=True)
        return
    model = parts[2].lower()
    if model not in MODELS:
        await callback.answer("Model topilmadi", show_alert=True)
        return
    await state.update_data(pollinations_model=model)
    await callback.answer("Model saqlandi")
    await _show_menu(callback, state)


@router.callback_query(F.data.startswith("pollinations:size:"))
async def pollinations_size_handler(
    callback: CallbackQuery, state: FSMContext
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
    await _show_menu(callback, state)


@router.message(PollinationsState.waiting_prompt, F.text & ~F.text.startswith("/"))
async def pollinations_prompt_handler(
    message: Message,
    state: FSMContext,
    ai_store: AIStore,
) -> None:
    prompt = (message.text or "").strip()
    if not prompt or prompt.startswith("/"):
        return

    model, size = _settings(await state.get_data())
    charge = await ensure_balance(
        ai_store,
        message,
        "pollinations_generate",
        reply_markup=pollinations_keyboard(model, size),
    )
    if charge is None:
        return
    _user, cost, user_id, username, full_name = charge
    seed = random.randint(1000, 9_999_999)
    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            image = await generate_image(
                prompt,
                model=model,
                width=size[0],
                height=size[1],
                seed=seed,
            )
    except Exception as error:  # noqa: BLE001
        logger.warning("Rasm yaratish xatosi: %s", error)
        await message.answer(
            f"<b>Rasm yaratish xatosi</b>\n{_public_generation_error()}",
            parse_mode="HTML",
            reply_markup=pollinations_keyboard(model, size),
        )
        return

    file_name = f"rasm_{model}_{size[0]}x{size[1]}_{seed}.png"
    photo = BufferedInputFile(image, filename=file_name)
    caption = (
        "<b>Rasm tayyor</b>\n"
        f"Model: <b>{html.escape(model.upper())}</b>\n"
        f"O'lcham: <b>{size[0]}x{size[1]}</b>\n"
        f"Seed: <code>{seed}</code>\n"
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
    await ai_store.charge_tokens(
        user_id=user_id,
        username=username,
        full_name=full_name,
        amount=cost,
    )
    await state.set_state(PollinationsState.waiting_prompt)


@router.message(PollinationsState.waiting_prompt)
async def pollinations_fallback(message: Message, state: FSMContext) -> None:
    model, size = _settings(await state.get_data())
    await message.answer(
        "Prompt sifatida oddiy matn yuboring.",
        reply_markup=pollinations_keyboard(model, size),
    )
