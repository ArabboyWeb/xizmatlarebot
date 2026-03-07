from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import Chat

DEFAULT_AI_LOG_CHANNEL_LINK = "https://t.me/+0IXGNITrmlNmZGMy"
DEFAULT_CHANNEL_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "ai_log_channel.json"
)
MAX_CHUNK_LENGTH = 3400


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def channel_link() -> str:
    return _env("AI_LOG_CHANNEL_LINK", DEFAULT_AI_LOG_CHANNEL_LINK)


def _state_path() -> Path:
    custom_path = _env("AI_LOG_CHANNEL_STATE_PATH")
    if custom_path:
        return Path(custom_path)
    return DEFAULT_CHANNEL_STATE_PATH


def _read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _write_state(payload: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def remember_channel(chat: Chat) -> None:
    if getattr(chat, "type", None) != "channel":
        return
    _write_state(
        {
            "chat_id": int(chat.id),
            "title": str(getattr(chat, "title", "") or "").strip(),
            "username": str(getattr(chat, "username", "") or "").strip(),
            "updated_at": _utc_now_text(),
        }
    )


def resolve_channel_target() -> str:
    explicit_target = _env("AI_LOG_CHANNEL_ID")
    if explicit_target:
        return explicit_target

    state = _read_state()
    chat_id = state.get("chat_id")
    if isinstance(chat_id, int):
        return str(chat_id)
    if isinstance(chat_id, str) and chat_id.strip():
        return chat_id.strip()

    link = channel_link()
    if link.startswith("https://t.me/") and "/+" not in link:
        tail = link.rsplit("/", 1)[-1].strip().lstrip("@")
        if tail:
            return f"@{tail}"
    return ""


def has_channel_target() -> bool:
    return bool(resolve_channel_target())


def _split_text(text: str, prefix: str) -> list[str]:
    escaped = html.escape(str(text or "").strip() or "-")
    chunks: list[str] = []
    while escaped:
        chunks.append(escaped[:MAX_CHUNK_LENGTH])
        escaped = escaped[MAX_CHUNK_LENGTH:]
    if not chunks:
        chunks = ["-"]
    if len(chunks) == 1:
        return [f"<b>{prefix}</b>\n<pre>{chunks[0]}</pre>"]
    return [
        f"<b>{prefix} [{index}/{len(chunks)}]</b>\n<pre>{chunk}</pre>"
        for index, chunk in enumerate(chunks, start=1)
    ]


def _user_label(*, user_id: int, username: str, full_name: str) -> str:
    label = full_name.strip() or username.strip() or str(user_id)
    if username.strip():
        label = f"{label} (@{username.strip()})"
    return label


async def log_ai_exchange(
    bot: Bot,
    *,
    user_id: int,
    username: str,
    full_name: str,
    prompt_text: str,
    answer_text: str,
    current_plan: str,
    effective_plan: str,
    model: str,
    credits_used: int,
    prompt_tokens: int,
    completion_tokens: int,
) -> bool:
    target = resolve_channel_target()
    if not target:
        return False

    label = _user_label(user_id=user_id, username=username, full_name=full_name)
    fallback_note = ""
    if current_plan != effective_plan:
        fallback_note = f"\n<b>Amaldagi rejim:</b> <b>{html.escape(effective_plan.title())}</b>"

    header = (
        "<b>AI chat log</b>\n"
        f"<b>Vaqt:</b> <code>{html.escape(_utc_now_text())}</code>\n"
        f"<b>Foydalanuvchi:</b> <a href=\"tg://user?id={user_id}\">{html.escape(label)}</a>\n"
        f"<b>User ID:</b> <code>{user_id}</code>\n"
        f"<b>Plan:</b> <b>{html.escape(current_plan.title())}</b>"
        f"{fallback_note}\n"
        f"<b>Model:</b> <code>{html.escape(model)}</code>\n"
        f"<b>Kredit:</b> <b>{credits_used}</b>\n"
        f"<b>Tokenlar:</b> <b>{prompt_tokens}</b> in / <b>{completion_tokens}</b> out"
    )
    await bot.send_message(
        chat_id=target,
        text=header,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    for chunk in _split_text(prompt_text, "Prompt"):
        await bot.send_message(
            chat_id=target,
            text=chunk,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    for chunk in _split_text(answer_text, "Javob"):
        await bot.send_message(
            chat_id=target,
            text=chunk,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    return True
