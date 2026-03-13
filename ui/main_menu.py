import logging
from typing import Literal

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from services.token_pricing import premium_monthly_credits

logger = logging.getLogger(__name__)

MenuSection = Literal["main", "ai", "media", "tools", "search", "cabinet"]


def _plan_label(plan: str) -> str:
    return "Premium" if str(plan or "free").strip().lower() == "premium" else "Free"


def _format_balance(balance: int) -> str:
    return f"{max(0, int(balance)):,}"


def _main_rows(is_admin: bool) -> list[list[InlineKeyboardButton]]:
    rows = [
        [InlineKeyboardButton(text="🤖 Sun'iy intellekt", callback_data="menu:section:ai")],
        [InlineKeyboardButton(text="🎬 Media Save", callback_data="menu:section:media")],
        [InlineKeyboardButton(text="🧰 Boshqa xizmatlar", callback_data="menu:section:tools")],
        [InlineKeyboardButton(text="👤 Kabinet", callback_data="menu:section:cabinet")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Admin panel", callback_data="admin:panel")])
    return rows


def _section_rows(
    section: MenuSection,
    *,
    referral_link: str = "",
) -> list[list[InlineKeyboardButton]]:
    if section == "ai":
        return [
            [InlineKeyboardButton(text="💬 AI Chat", callback_data="services:ai")],
            [InlineKeyboardButton(text="🎨 Rasm yaratish", callback_data="services:pollinations")],
            [InlineKeyboardButton(text="📄 PDF / Doc konvertor", callback_data="services:converter")],
            [InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="menu:main")],
        ]
    if section == "media":
        return [
            [InlineKeyboardButton(text="📥 YT / Insta / TikTok", callback_data="services:youtube")],
            [InlineKeyboardButton(text="📎 Fayl saqlash", callback_data="services:save")],
            [InlineKeyboardButton(text="🎵 Musiqa aniqlash", callback_data="services:shazam")],
            [InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="menu:main")],
        ]
    if section == "tools":
        return [
            [InlineKeyboardButton(text="💱 Valyuta", callback_data="services:currency")],
            [InlineKeyboardButton(text="🌦 Ob-havo", callback_data="services:weather")],
            [InlineKeyboardButton(text="🌐 Tarjimon", callback_data="services:translate")],
            [InlineKeyboardButton(text="🔗 Link qisqartirish", callback_data="services:tinyurl")],
            [InlineKeyboardButton(text="🔄 Konvertor", callback_data="services:converter")],
            [InlineKeyboardButton(text="💼 Ish qidirish", callback_data="services:jobs")],
            [InlineKeyboardButton(text="📚 Wikipedia", callback_data="services:wikipedia")],
            [InlineKeyboardButton(text="✉️ Temp Mail", callback_data="services:tempmail")],
            [InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="menu:main")],
        ]
    if section == "search":
        return [
            [InlineKeyboardButton(text="💼 Ish qidirish", callback_data="services:jobs")],
            [InlineKeyboardButton(text="📚 Wikipedia", callback_data="services:wikipedia")],
            [InlineKeyboardButton(text="✉️ Temp Mail", callback_data="services:tempmail")],
            [InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="menu:main")],
        ]
    if section == "cabinet":
        rows = [
            [InlineKeyboardButton(text="💎 Premium", callback_data="premium:page")],
            [InlineKeyboardButton(text="🎁 Referral markazi", callback_data="cabinet:referral")],
            [InlineKeyboardButton(text="⬅️ Bosh menyu", callback_data="menu:main")],
        ]
        if referral_link.startswith("https://t.me/"):
            rows.insert(2, [InlineKeyboardButton(text="🔗 Referral linkni ochish", url=referral_link)])
        return rows
    return _main_rows(False)


def services_keyboard(
    is_admin: bool = False,
    *,
    section: MenuSection = "main",
    referral_link: str = "",
) -> InlineKeyboardMarkup:
    rows = _main_rows(is_admin) if section == "main" else _section_rows(section, referral_link=referral_link)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_text(
    upload_limit_bytes: int,
    download_limit_bytes: int,
    *,
    user_plan: str = "free",
    token_balance: int = 0,
    referral_count: int = 0,
    referral_link: str = "",
    referrer_id: int = 0,
    lifetime_tokens_earned: int = 0,
    lifetime_tokens_spent: int = 0,
    referral_inviter_bonus: int = 0,
    referral_invitee_bonus: int = 0,
    free_reset_tokens: int = 0,
    free_reset_hours: int = 0,
    free_reset_date: str = "",
    reset_date: str = "",
    notice: str = "",
    is_admin: bool = False,
    **_: object,
) -> str:
    _ = (
        upload_limit_bytes,
        download_limit_bytes,
        referral_link,
        free_reset_tokens,
        free_reset_hours,
        free_reset_date,
        reset_date,
        is_admin,
    )
    text = (
        "<b>👋 Xizmatlar E-Bot</b>\n"
        "Kerakli bo'limni tanlang.\n\n"
        f"👑 Reja: <b>{_plan_label(user_plan)}</b>\n"
        f"💳 Balans: <b>{_format_balance(token_balance)} kredit</b>"
    )
    if notice:
        text += f"\n\n{notice}"
    return text


def section_menu_text(
    section: MenuSection,
    *,
    user_plan: str = "free",
    token_balance: int = 0,
    referral_count: int = 0,
    referral_link: str = "",
    referrer_id: int = 0,
    lifetime_tokens_earned: int = 0,
    lifetime_tokens_spent: int = 0,
    referral_inviter_bonus: int = 0,
    referral_invitee_bonus: int = 0,
    free_reset_tokens: int = 0,
    free_reset_hours: int = 0,
    free_reset_date: str = "",
    reset_date: str = "",
) -> str:
    _ = (referral_link, free_reset_tokens, free_reset_hours)
    if section == "ai":
        return (
            "<b>🤖 Sun'iy intellekt</b>\n"
            "AI chat, rasm yaratish va fayl konvertor bo'limi."
        )
    if section == "media":
        return (
            "<b>🎬 Media Save</b>\n"
            "Video, audio va fayl saqlash xizmatlari shu yerda."
        )
    if section == "tools":
        return (
            "<b>🧰 Boshqa xizmatlar</b>\n"
            "Tarjima, ob-havo, valyuta, qidiruv va boshqa foydali servislar."
        )
    if section == "search":
        return (
            "<b>🔎 Qidiruv</b>\n"
            "Kerakli ma'lumotni topish uchun xizmatni tanlang."
        )
    if section == "cabinet":
        reset_label = (
            str(free_reset_date or "").replace("T", " ")[:16]
            if str(user_plan or "").strip().lower() == "free"
            else str(reset_date or "").replace("T", " ")[:16]
        )
        text = (
            "<b>👤 Kabinet</b>\n"
            "Hisobingiz bo'yicha asosiy ma'lumotlar.\n\n"
            f"👑 Reja: <b>{_plan_label(user_plan)}</b>\n"
            f"💳 Balans: <b>{_format_balance(token_balance)} kredit</b>\n"
            f"🎁 Referral: <b>{max(0, int(referral_count))} ta</b>\n"
            f"📈 Olingan: <b>{_format_balance(lifetime_tokens_earned)} kredit</b>\n"
            f"📉 Sarflangan: <b>{_format_balance(lifetime_tokens_spent)} kredit</b>"
        )
        if str(user_plan or "").strip().lower() == "premium":
            text += f"\n\n💎 Premium limiti: <b>{premium_monthly_credits()} kredit / oy</b>"
        elif reset_label:
            text += f"\n\n⏳ Keyingi free reset: <b>{reset_label}</b>"
        if referrer_id > 0:
            text += f"\n\n👥 Taklif qilgan ID: <code>{int(referrer_id)}</code>"
        if referral_inviter_bonus > 0 or referral_invitee_bonus > 0:
            text += (
                "\n\nReferral bonuslari:"
                f"\n- Do'stingizga: <b>{int(referral_invitee_bonus)}</b> kredit"
                f"\n- Sizga: <b>{int(referral_inviter_bonus)}</b> kredit"
            )
        return text
    return ""


def referral_menu_text(
    *,
    referral_count: int = 0,
    referral_link: str = "",
    referrer_id: int = 0,
    referral_inviter_bonus: int = 0,
    referral_invitee_bonus: int = 0,
    free_reset_tokens: int = 0,
    free_reset_hours: int = 0,
    free_reset_date: str = "",
    **_: object,
) -> str:
    _ = (free_reset_tokens, free_reset_hours)
    rows = [
        "<b>🎁 Referral markazi</b>",
        "",
        f"Taklif qilgan do'stlaringiz: <b>{max(0, int(referral_count))} ta</b>",
    ]
    if referrer_id > 0:
        rows.append(f"Taklif qilgan ID: <code>{int(referrer_id)}</code>")
    if referral_inviter_bonus > 0 or referral_invitee_bonus > 0:
        rows.extend(
            [
                "",
                "Referral bonuslari:",
                f"- Do'stingizga: <b>{int(referral_invitee_bonus)}</b> kredit",
                f"- Sizga: <b>{int(referral_inviter_bonus)}</b> kredit",
            ]
        )
    if referral_link:
        rows.extend(["", f"Referral link: <code>{referral_link}</code>"])
    refill_label = str(free_reset_date or "").replace("T", " ")[:16]
    if refill_label:
        rows.append(f"⏳ Keyingi free reset: <b>{refill_label}</b>")
    rows.extend(
        [
            "",
            "Linkni bitta tugma bilan nusxalab yuboring.",
            "Do'stingiz shu link orqali kirsa, ikkalangiz ham bonus olasiz.",
        ]
    )
    return "\n".join(rows)


def referral_keyboard(referral_link: str = "") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if referral_link:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📋 Referral linkni nusxalash",
                    copy_text=CopyTextButton(text=referral_link),
                )
            ]
        )
    if referral_link.startswith("https://t.me/"):
        rows.append([InlineKeyboardButton(text="🔗 Referral linkni ochish", url=referral_link)])
    rows.append([InlineKeyboardButton(text="👤 Kabinet", callback_data="menu:section:cabinet")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def safe_edit_menu(
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
            logger.warning("Menu edit failed: %s", error)
            await callback.message.answer(
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
