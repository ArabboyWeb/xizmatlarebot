import logging
from typing import Literal

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from services.token_pricing import premium_daily_tokens

logger = logging.getLogger(__name__)

MenuSection = Literal["main", "ai", "media", "tools", "search", "cabinet"]


def _plan_label(plan: str) -> str:
    normalized = str(plan or "free").strip().lower()
    if normalized == "premium":
        return "Premium"
    return "Free"


def _format_balance(balance: int) -> str:
    return f"{max(0, int(balance)):,}"


def _main_rows(is_admin: bool) -> list[list[InlineKeyboardButton]]:
    rows = [
        [InlineKeyboardButton(text="💎 AI", callback_data="menu:section:ai")],
        [
            InlineKeyboardButton(
                text="📥 Media va yuklab olish",
                callback_data="menu:section:media",
            ),
            InlineKeyboardButton(
                text="🛠 Foydali asboblar",
                callback_data="menu:section:tools",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔍 Qidiruv",
                callback_data="menu:section:search",
            ),
            InlineKeyboardButton(
                text="👤 Kabinet",
                callback_data="menu:section:cabinet",
            ),
        ],
        [InlineKeyboardButton(text="⭐ Premium", callback_data="premium:page")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="Admin panel", callback_data="admin:panel")])
    return rows


def _section_rows(
    section: MenuSection,
    *,
    referral_link: str = "",
) -> list[list[InlineKeyboardButton]]:
    if section == "ai":
        return [
            [
                InlineKeyboardButton(text="💬 AI Chat", callback_data="services:ai"),
                InlineKeyboardButton(
                    text="🎨 Rasm yaratish",
                    callback_data="services:pollinations",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📄 PDF/Doc konvertor",
                    callback_data="services:converter",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:main")],
        ]
    if section == "media":
        return [
            [
                InlineKeyboardButton(
                    text="🎥 YT / Insta / TikTok saver",
                    callback_data="services:youtube",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💾 Fayl saqlash",
                    callback_data="services:save",
                ),
                InlineKeyboardButton(
                    text="🎵 Musiqa qidirish",
                    callback_data="services:shazam",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:main")],
        ]
    if section == "tools":
        return [
            [
                InlineKeyboardButton(
                    text="💱 Valyuta kursi",
                    callback_data="services:currency",
                ),
                InlineKeyboardButton(
                    text="☁️ Ob-havo",
                    callback_data="services:weather",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌐 Tarjimon",
                    callback_data="services:translate",
                ),
                InlineKeyboardButton(
                    text="🔗 Link qisqartirish",
                    callback_data="services:tinyurl",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📄 Konvertor",
                    callback_data="services:converter",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:main")],
        ]
    if section == "search":
        return [
            [
                InlineKeyboardButton(
                    text="💼 Ish qidirish",
                    callback_data="services:jobs",
                ),
                InlineKeyboardButton(
                    text="📖 Wikipedia",
                    callback_data="services:wikipedia",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📧 Vaqtinchalik pochta",
                    callback_data="services:tempmail",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:main")],
        ]
    if section == "cabinet":
        rows = [
            [
                InlineKeyboardButton(
                    text="👥 Referral markazi",
                    callback_data="cabinet:referral",
                )
            ],
            [
                InlineKeyboardButton(text="💬 AI Chat", callback_data="services:ai"),
                InlineKeyboardButton(text="⭐ Premium", callback_data="premium:page"),
            ],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:main")],
        ]
        if referral_link.startswith("https://t.me/"):
            rows.insert(
                1,
                [InlineKeyboardButton(text="🔗 Referral linkni ochish", url=referral_link)],
            )
        return rows
    return _main_rows(False)


def services_keyboard(
    is_admin: bool = False,
    *,
    section: MenuSection = "main",
    referral_link: str = "",
) -> InlineKeyboardMarkup:
    rows = (
        _main_rows(is_admin)
        if section == "main"
        else _section_rows(section, referral_link=referral_link)
    )
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
        "<b>Assalomu alaykum. Xizmatlar E-Botga xush kelibsiz.</b>\n"
        "Pastdagi bolimlardan birini tanlang.\n\n"
        f"👤 Holat: <b>{_plan_label(user_plan)}</b>\n"
        f"💎 Balans: <b>{_format_balance(token_balance)} token</b>\n"
        f"👫 Taklif qilgan dostlaringiz: <b>{max(0, int(referral_count))} ta</b>\n\n"
        "Premium sahifasida tolov, karta va tasdiqlash jarayonini korishingiz mumkin."
    )
    if notice:
        text += f"\n\n{notice}"
    if referrer_id > 0:
        text += f"\n\n👥 Sizni taklif qilgan ID: <code>{int(referrer_id)}</code>"
    if lifetime_tokens_earned > 0 or lifetime_tokens_spent > 0:
        text += (
            f"\n📈 Olingan token: <b>{_format_balance(lifetime_tokens_earned)}</b>"
            f"\n📉 Sarflangan token: <b>{_format_balance(lifetime_tokens_spent)}</b>"
        )
    if referral_inviter_bonus > 0 or referral_invitee_bonus > 0:
        text += (
            "\n\n🎁 Referral bonuslari:"
            f"\n- Dostingizga: <b>{int(referral_invitee_bonus)}</b> token"
            f"\n- Sizga: <b>{int(referral_inviter_bonus)}</b> token"
        )
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
    _ = referral_link
    if section == "ai":
        return (
            "<b>💎 AI</b>\n"
            "Kerakli AI bolimini tanlang.\n\n"
            "💬 AI Chat\n"
            "🎨 Rasm yaratish\n"
            "📄 PDF/Doc konvertor"
        )
    if section == "media":
        return (
            "<b>📥 Media va yuklab olish</b>\n"
            "Media va yuklab olish xizmatlari shu yerda.\n\n"
            "🎥 YT / Instagram / TikTok saver\n"
            "💾 Fayl saqlash\n"
            "🎵 Musiqa qidirish"
        )
    if section == "tools":
        return (
            "<b>🛠 Foydali asboblar</b>\n"
            "Tez ishlatiladigan yordamchi xizmatlar.\n\n"
            "💱 Valyuta kursi va konvertor\n"
            "☁️ Ob-havo malumoti\n"
            "🌐 Tarjimon\n"
            "🔗 Link qisqartirish\n"
            "📄 Fayl konvertori"
        )
    if section == "search":
        return (
            "<b>🔍 Malumot qidirish</b>\n"
            "Kerakli malumotni topish uchun bolimni tanlang.\n\n"
            "💼 Ish qidirish\n"
            "📖 Wikipedia\n"
            "📧 Vaqtinchalik pochta"
        )
    if section == "cabinet":
        text = (
            "<b>👤 Kabinet / Balans</b>\n"
            "Hisobingiz boyicha qisqa malumot.\n\n"
            f"👤 Holat: <b>{_plan_label(user_plan)}</b>\n"
            f"💎 Balans: <b>{_format_balance(token_balance)} token</b>\n"
            f"👫 Referral: <b>{max(0, int(referral_count))} ta</b>\n"
            f"📈 Olingan: <b>{_format_balance(lifetime_tokens_earned)} token</b>\n"
            f"📉 Sarflangan: <b>{_format_balance(lifetime_tokens_spent)} token</b>\n\n"
            "Referral markazi va Premium uchun tugmalardan foydalaning."
        )
        reset_label = (
            str(free_reset_date or "").replace("T", " ")[:16]
            if str(user_plan or "").strip().lower() == "free"
            else str(reset_date or "").replace("T", " ")[:16]
        )
        if str(user_plan or "").strip().lower() == "free":
            if free_reset_tokens > 0 and free_reset_hours > 0:
                text += (
                    f"\n\n🔄 Free refill: <b>{int(free_reset_tokens)} token / "
                    f"{int(free_reset_hours)} soat</b>"
                )
        else:
            text += (
                f"\n\n🔄 Premium refill: <b>{premium_daily_tokens()} token / "
                f"{int(free_reset_hours)} soat</b>"
            )
        if reset_label:
            text += f"\n⏱ Keyingi refill: <b>{reset_label}</b>"
        if referrer_id > 0:
            text += f"\n\n👥 Sizni taklif qilgan ID: <code>{int(referrer_id)}</code>"
        if referral_inviter_bonus > 0 or referral_invitee_bonus > 0:
            text += (
                "\n\n🎁 Referral bonuslari:"
                f"\n- Dostingizga: <b>{int(referral_invitee_bonus)}</b> token"
                f"\n- Sizga: <b>{int(referral_inviter_bonus)}</b> token"
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
    rows = [
        "<b>👥 Referral markazi</b>",
        "",
        f"👫 Taklif qilgan dostlaringiz: <b>{max(0, int(referral_count))} ta</b>",
    ]
    if referrer_id > 0:
        rows.append(f"👥 Sizni taklif qilgan ID: <code>{int(referrer_id)}</code>")
    if referral_inviter_bonus > 0 or referral_invitee_bonus > 0:
        rows.append("")
        rows.append("🎁 Referral bonuslari:")
        rows.append(f"- Dostingizga: <b>{int(referral_invitee_bonus)}</b> token")
        rows.append(f"- Sizga: <b>{int(referral_inviter_bonus)}</b> token")
    if referral_link:
        rows.append("")
        rows.append(f"🔗 Referral link: <code>{referral_link}</code>")
    if free_reset_tokens > 0 and free_reset_hours > 0:
        rows.append("")
        rows.append(
            f"🔄 Free refill: <b>{int(free_reset_tokens)} token / {int(free_reset_hours)} soat</b>"
        )
    refill_label = str(free_reset_date or "").replace("T", " ")[:16]
    if refill_label:
        rows.append(f"⏱ Keyingi refill: <b>{refill_label}</b>")
    rows.append("")
    rows.append("Linkni bir tugma bilan nusxalab yuboring.")
    rows.append("Dostingiz sizning linkingiz orqali kirsa, ikkalangiz ham bonus olasiz.")
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
    rows.append([InlineKeyboardButton(text="⬅️ Kabinet", callback_data="menu:section:cabinet")])
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
