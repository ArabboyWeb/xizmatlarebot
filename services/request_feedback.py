from __future__ import annotations

import contextlib

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

DEFAULT_WAIT_TEXT = "<b>Iltimos kuting...</b>\nSo'rov qayta ishlanmoqda."


async def send_wait_message(
    message: Message,
    *,
    text: str = DEFAULT_WAIT_TEXT,
) -> Message | None:
    try:
        return await message.answer(text, parse_mode="HTML")
    except Exception:
        return None


async def clear_wait_message(progress_message: Message | None) -> None:
    if progress_message is None:
        return
    with contextlib.suppress(TelegramBadRequest):
        await progress_message.delete()


async def edit_wait_message(
    progress_message: Message | None,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    if progress_message is None:
        return False
    try:
        await progress_message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        return True
    except TelegramBadRequest:
        return False
