from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import Message

GROUP_CHAT_TYPES = {"group", "supergroup"}
_PATCHED = False


def is_group_chat(target: object) -> bool:
    chat = getattr(target, "chat", None)
    if chat is None:
        message = getattr(target, "message", None)
        chat = getattr(message, "chat", None)
    return str(getattr(chat, "type", "") or "").strip().lower() in GROUP_CHAT_TYPES


def command_menu_text(*, is_admin: bool = False) -> str:
    rows = [
        "<b>Group command mode</b>",
        "",
        "Bu chatda inline tugmalar o'chirilgan.",
        "Servislarni slash command bilan ishlating:",
        "",
        "/ai - sun'iy intellekt",
        "/image - rasm yaratish",
        "/youtube - YT / Insta / TikTok saver",
        "/save - direct fayl saqlash",
        "/weather - ob-havo",
        "/currency - valyuta",
        "/translate - tarjimon",
        "/jobs - ish qidirish",
        "/wiki - wikipedia",
        "/mail - temporary email",
        "/mailread - mailbox xabarini o'qish",
        "/tinyurl - link qisqartirish",
        "/shazam - musiqa qidirish",
        "/convert - converter",
        "/word2pdf - Word -> PDF",
        "/pdf2word - PDF -> Word",
        "/img2pdf - Image -> PDF",
        "/pdf2img - PDF -> Images",
        "/imgpng - Image -> PNG",
        "/imgjpg - Image -> JPG",
        "/imgwebp - Image -> WEBP",
    ]
    if is_admin:
        rows.extend(["", "/admin - admin panel"])
    rows.extend(
        [
            "",
            "Bir command yuboring, keyingi xabarlar shu servisga tegishli bo'ladi.",
        ]
    )
    return "\n".join(rows)


def _strip_group_reply_markup(
    original: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    async def wrapped(self: Message, *args: Any, **kwargs: Any) -> Any:
        if is_group_chat(self):
            kwargs.pop("reply_markup", None)
        return await original(self, *args, **kwargs)

    return wrapped


def install_group_command_mode() -> None:
    global _PATCHED
    if _PATCHED:
        return

    for method_name in (
        "answer",
        "answer_photo",
        "answer_audio",
        "answer_video",
        "answer_document",
        "answer_animation",
        "edit_text",
        "edit_caption",
    ):
        original = getattr(Message, method_name, None)
        if callable(original):
            setattr(Message, method_name, _strip_group_reply_markup(original))

    _PATCHED = True
