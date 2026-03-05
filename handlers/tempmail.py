import html
import logging
import re

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

from services.tempmail_client import (
    create_mailbox,
    fetch_inbox,
    read_message,
    split_mailbox,
)

router = Router(name="tempmail")
logger = logging.getLogger(__name__)
TEMPMAIL_EMAIL_KEY = "tempmail_email"
MAX_INBOX_ITEMS = 10
MAX_BODY_CHARS = 2400


class TempMailState(StatesGroup):
    waiting_message_id = State()


def tempmail_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yangi email", callback_data="tempmail:new")],
            [InlineKeyboardButton(text="Inboxni yangilash", callback_data="tempmail:inbox")],
            [InlineKeyboardButton(text="Xabar ID oqish", callback_data="tempmail:read")],
            [InlineKeyboardButton(text="Orqaga", callback_data="services:back")],
        ]
    )


def read_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Inboxni yangilash", callback_data="tempmail:inbox")],
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
            logger.warning("Tempmail edit xatosi: %s", error)


def _extract_body(payload: dict[str, object]) -> str:
    for key in ("textBody", "body", "htmlBody"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            body = value.strip()
            if key == "htmlBody":
                body = re.sub(r"<[^>]+>", " ", body)
            body = " ".join(body.split())
            return body[:MAX_BODY_CHARS]
    return "Xabar matni topilmadi."


async def _ensure_mailbox(state: FSMContext) -> str:
    data = await state.get_data()
    current = str(data.get(TEMPMAIL_EMAIL_KEY, "")).strip().lower()
    if current:
        split_mailbox(current)
        return current
    mailbox = await create_mailbox()
    await state.update_data(tempmail_email=mailbox)
    return mailbox


def _build_inbox_text(email: str, messages: list[object]) -> str:
    header = (
        "<b>1secmail Inbox</b>\n"
        f"<b>Email:</b> <code>{html.escape(email)}</code>\n\n"
    )
    if not messages:
        return (
            header
            + "Hozircha xabar yo'q.\n"
            "Yangi xabar kelgach, <b>Inboxni yangilash</b> tugmasini bosing."
        )

    rows: list[str] = []
    for item in messages[:MAX_INBOX_ITEMS]:
        row = (
            f"#{item.message_id} | {html.escape(item.from_email)}\n"
            f"{html.escape(item.subject)}\n"
            f"<code>{html.escape(item.date)}</code>"
        )
        rows.append(row)

    if len(messages) > MAX_INBOX_ITEMS:
        rows.append(f"... va yana {len(messages) - MAX_INBOX_ITEMS} ta xabar")

    return (
        header
        + "<b>Xabarlar:</b>\n"
        + "\n\n".join(rows)
        + "\n\nXabarni ochish uchun <b>Xabar ID oqish</b> tugmasini bosing."
    )


@router.callback_query(F.data == "services:tempmail")
async def tempmail_entry_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await _safe_edit(
        callback,
        (
            "<b>1secmail (Temporary Email)</b>\n"
            "Free disposable email yarating va inboxni bot ichida tekshiring.\n"
            "Boshlash uchun <b>Yangi email</b> tugmasini bosing."
        ),
        tempmail_keyboard(),
    )


@router.callback_query(F.data == "tempmail:new")
async def tempmail_new_email_handler(
    callback: CallbackQuery, state: FSMContext
) -> None:
    if callback.message is None:
        await callback.answer()
        return
    await callback.answer("Mailbox yaratilmoqda...")
    try:
        async with ChatActionSender.typing(
            bot=callback.bot, chat_id=callback.message.chat.id
        ):
            mailbox = await create_mailbox()
        await state.update_data(tempmail_email=mailbox)
        await _safe_edit(
            callback,
            (
                "<b>Yangi mailbox tayyor</b>\n"
                f"<code>{html.escape(mailbox)}</code>\n\n"
                "Inboxni tekshirish uchun pastdagi tugmadan foydalaning."
            ),
            tempmail_keyboard(),
        )
    except Exception as error:  # noqa: BLE001
        await _safe_edit(
            callback,
            f"<b>1secmail xatosi</b>\n{html.escape(str(error))}",
            tempmail_keyboard(),
        )


@router.callback_query(F.data == "tempmail:inbox")
async def tempmail_inbox_handler(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is None:
        await callback.answer()
        return
    await callback.answer("Inbox yangilanmoqda...")
    try:
        async with ChatActionSender.typing(
            bot=callback.bot, chat_id=callback.message.chat.id
        ):
            mailbox = await _ensure_mailbox(state)
            messages = await fetch_inbox(mailbox)
        await _safe_edit(callback, _build_inbox_text(mailbox, messages), tempmail_keyboard())
    except Exception as error:  # noqa: BLE001
        await _safe_edit(
            callback,
            f"<b>Inbox xatosi</b>\n{html.escape(str(error))}",
            tempmail_keyboard(),
        )


@router.callback_query(F.data == "tempmail:read")
async def tempmail_read_prompt_handler(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await callback.answer()
    try:
        mailbox = await _ensure_mailbox(state)
    except Exception as error:  # noqa: BLE001
        await _safe_edit(
            callback,
            f"<b>Mailbox xatosi</b>\n{html.escape(str(error))}",
            tempmail_keyboard(),
        )
        return

    await state.set_state(TempMailState.waiting_message_id)
    await _safe_edit(
        callback,
        (
            "<b>Xabar ID kiriting</b>\n"
            f"Joriy email: <code>{html.escape(mailbox)}</code>\n"
            "Masalan: <code>123456789</code>"
        ),
        read_keyboard(),
    )


@router.message(TempMailState.waiting_message_id, F.text)
async def tempmail_read_message_handler(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw or raw.startswith("/"):
        return

    try:
        message_id = int(raw)
    except ValueError:
        await message.answer(
            "Faqat xabar ID raqamini yuboring.",
            reply_markup=read_keyboard(),
        )
        return

    try:
        async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
            mailbox = await _ensure_mailbox(state)
            payload = await read_message(mailbox, message_id=message_id)
    except Exception as error:  # noqa: BLE001
        await message.answer(
            f"<b>Xabarni o'qishda xato</b>\n{html.escape(str(error))}",
            parse_mode="HTML",
            reply_markup=read_keyboard(),
        )
        return

    from_email = html.escape(str(payload.get("from", "")).strip() or "Nomalum")
    subject = html.escape(str(payload.get("subject", "")).strip() or "(No subject)")
    date_value = html.escape(str(payload.get("date", "")).strip() or "Nomalum")
    body_text = html.escape(_extract_body(payload))
    attachment_lines: list[str] = []
    attachments = payload.get("attachments")
    if isinstance(attachments, list) and attachments:
        for item in attachments[:5]:
            if not isinstance(item, dict):
                continue
            filename = html.escape(str(item.get("filename", "file")).strip())
            size = html.escape(str(item.get("size", "")).strip())
            attachment_lines.append(f"- {filename} ({size} bytes)")

    response_text = (
        "<b>Inbox xabari</b>\n"
        f"<b>ID:</b> <code>{message_id}</code>\n"
        f"<b>Email:</b> <code>{html.escape(mailbox)}</code>\n"
        f"<b>Kimdan:</b> {from_email}\n"
        f"<b>Mavzu:</b> {subject}\n"
        f"<b>Sana:</b> <code>{date_value}</code>\n\n"
        f"<b>Matn:</b>\n{body_text}"
    )
    if attachment_lines:
        response_text += "\n\n<b>Attachmentlar:</b>\n" + "\n".join(attachment_lines)

    await message.answer(
        response_text,
        parse_mode="HTML",
        reply_markup=read_keyboard(),
    )


@router.message(TempMailState.waiting_message_id)
async def tempmail_read_fallback(message: Message) -> None:
    await message.answer(
        "Xabarni ochish uchun raqamli message ID yuboring.",
        reply_markup=read_keyboard(),
    )
