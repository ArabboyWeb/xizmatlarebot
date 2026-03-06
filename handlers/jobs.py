import html
import logging

from aiogram import F, Router
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

from services.jsearch_client import search_jobs

router = Router(name="jobs")
logger = logging.getLogger(__name__)


class JobsState(StatesGroup):
    waiting_query = State()


def jobs_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")]
        ]
    )


def jobs_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yana qidirish", callback_data="jobs:start")],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
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
            logger.warning("Jobs edit xatosi: %s", error)


async def _show_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(JobsState.waiting_query)
    await _safe_edit(
        callback,
        (
            "<b>Jobs Search</b>\n"
            "RapidAPI ishlamasa free public jobs fallback ishlaydi.\n"
            "Kasb va joylashuv bo'yicha qidiring.\n"
            "Masalan: <code>developer jobs in chicago</code>"
        ),
        jobs_prompt_keyboard(),
    )


def _build_jobs_text(query: str, jobs: list[dict[str, str]]) -> str:
    text = [f"<b>JSearch natijalari</b>\nSo'rov: <code>{html.escape(query)}</code>"]
    if not jobs:
        text.append("\nNatija topilmadi.")
        return "\n".join(text)

    text.append("")
    for idx, job in enumerate(jobs[:8], start=1):
        title = html.escape(job.get("title", "Job"))
        company = html.escape(job.get("company", ""))
        location = html.escape(job.get("location", ""))
        apply_link = html.escape(job.get("apply_link", ""))
        posted = html.escape(job.get("posted", ""))
        row = [f"{idx}. <b>{title}</b>"]
        if company:
            row.append(f"   {company}")
        if location:
            row.append(f"   {location}")
        if posted:
            row.append(f"   Posted: <code>{posted}</code>")
        if apply_link:
            row.append(f"   {apply_link}")
        text.append("\n".join(row))
        text.append("")
    return "\n".join(text).strip()


@router.callback_query(F.data == "services:jobs")
async def jobs_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _show_prompt(callback, state)


@router.callback_query(F.data == "jobs:start")
async def jobs_start_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_prompt(callback, state)


@router.message(JobsState.waiting_query, F.text)
async def jobs_query_handler(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if not query or query.startswith("/"):
        return

    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            data = await search_jobs(query)
    except Exception as error:  # noqa: BLE001
        await message.answer(
            f"<b>JSearch xatosi</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=jobs_prompt_keyboard(),
        )
        return

    await state.set_state(JobsState.waiting_query)
    await message.answer(
        _build_jobs_text(data.get("query", query), list(data.get("jobs", []))),
        parse_mode="HTML",
        reply_markup=jobs_result_keyboard(),
    )


@router.message(JobsState.waiting_query)
async def jobs_fallback(message: Message) -> None:
    await message.answer(
        "Ish qidiruvi uchun matn yuboring.",
        reply_markup=jobs_prompt_keyboard(),
    )
