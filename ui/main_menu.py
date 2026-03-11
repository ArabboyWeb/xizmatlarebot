import logging
from typing import Literal

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

MenuSection = Literal["main", "ai", "media", "tools", "search", "cabinet"]


def _plan_label(plan: str) -> str:
    normalized = str(plan or "free").strip().lower()
    if normalized == "premium":
        return "Premium"
    return "Free (Oddiy)"


def _format_balance(balance: int) -> str:
    return f"{max(0, int(balance)):,}"


def _main_rows(is_admin: bool) -> list[list[InlineKeyboardButton]]:
    rows = [
        [InlineKeyboardButton(text="💎 Sun'iy Intellekt", callback_data="menu:section:ai")],
        [
            InlineKeyboardButton(
                text="📥 Media & Yuklab olish",
                callback_data="menu:section:media",
            ),
            InlineKeyboardButton(
                text="🛠 Foydali Asboblar",
                callback_data="menu:section:tools",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔍 Ma'lumot qidirish",
                callback_data="menu:section:search",
            ),
            InlineKeyboardButton(
                text="👤 Kabinet / Balans",
                callback_data="menu:section:cabinet",
            ),
        ],
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
                    text="🎨 Rasm Yaratish",
                    callback_data="services:pollinations",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📄 PDF/Doc Konvertor",
                    callback_data="services:converter",
                ),
                InlineKeyboardButton(
                    text="💳 Premiumga o'tish",
                    callback_data="ai:plans",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:main")],
        ]
    if section == "media":
        return [
            [
                InlineKeyboardButton(
                    text="🎥 YT / Insta / TikTok Saver",
                    callback_data="services:youtube",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💾 Fayllarni saqlash",
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
                    text="📧 Vaqtinchalik Pochta",
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
                InlineKeyboardButton(
                    text="💳 Balans tafsiloti",
                    callback_data="services:ai",
                )
            ],
            [
                InlineKeyboardButton(text="💬 AI Chat", callback_data="services:ai"),
                InlineKeyboardButton(
                    text="⭐ Premium tariflar",
                    callback_data="ai:plans",
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:main")],
        ]
        if referral_link.startswith("https://t.me/"):
            rows.insert(
                1,
                [InlineKeyboardButton(text="🔗 Referral link", url=referral_link)],
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
        free_reset_tokens,
        free_reset_hours,
        free_reset_date,
        reset_date,
        is_admin,
    )
    text = (
        "<b>Assalomu alaykum! Xizmatlar E-Botga xush kelibsiz.</b>\n"
        "Pastdagi bo'limlardan birini tanlang.\n\n"
        f"👤 Sizning holatingiz: <b>{_plan_label(user_plan)}</b>\n"
        f"💎 Balansingiz: <b>{_format_balance(token_balance)} token</b>\n"
        f"👫 Taklif qilgan do'stlaringiz: <b>{max(0, int(referral_count))} ta</b>\n\n"
        "AI bilan cheksiz muloqot va yuqori tezlik uchun "
        "<b>Premium</b> tarifni faollashtiring!"
    )
    if notice:
        text += f"\n\n{notice}"
    if referrer_id > 0:
        text += f"\n\n👥 Sizni taklif qilgan ID: <code>{int(referrer_id)}</code>"
    if lifetime_tokens_earned > 0 or lifetime_tokens_spent > 0:
        text += (
            f"\n📈 Umumiy olingan token: <b>{_format_balance(lifetime_tokens_earned)}</b>"
            f"\n📉 Umumiy sarflangan token: <b>{_format_balance(lifetime_tokens_spent)}</b>"
        )
    if referral_inviter_bonus > 0 or referral_invitee_bonus > 0:
        text += (
            "\n\n🎁 Referal bonuslari:"
            f"\n- Do'stingizga: <b>{int(referral_invitee_bonus)}</b> token"
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
    if section == "ai":
        return (
            "<b>💎 Sun'iy Intellekt</b>\n"
            "Kerakli AI bo'limini tanlang.\n\n"
            "💬 AI Chat\n"
            "🎨 Rasm Yaratish\n"
            "📄 PDF/Doc Konvertor\n"
            "💳 Premium tariflar"
        )
    if section == "media":
        return (
            "<b>📥 Media & Yuklab olish</b>\n"
            "Media va yuklab olish xizmatlari shu yerda.\n\n"
            "🎥 YT / Instagram / TikTok Saver\n"
            "💾 Fayllarni saqlash\n"
            "🎵 Musiqa qidirish"
        )
    if section == "tools":
        return (
            "<b>🛠 Foydali Asboblar</b>\n"
            "Tez-tez ishlatiladigan yordamchi xizmatlar.\n\n"
            "💱 Valyuta kursi va konvertor\n"
            "☁️ Ob-havo ma'lumoti\n"
            "🌐 Tarjimon\n"
            "🔗 Link qisqartirish\n"
            "📄 Fayl konvertori"
        )
    if section == "search":
        return (
            "<b>🔍 Ma'lumot qidirish</b>\n"
            "Kerakli ma'lumotni topish uchun bo'limni tanlang.\n\n"
            "💼 Ish qidirish\n"
            "📖 Wikipedia\n"
            "📧 Vaqtinchalik pochta"
        )
    if section == "cabinet":
        text = (
            "<b>👤 Kabinet / Balans</b>\n"
            "Hisobingiz bo'yicha qisqa ma'lumot.\n\n"
            f"👤 Holat: <b>{_plan_label(user_plan)}</b>\n"
            f"💎 Balans: <b>{_format_balance(token_balance)} token</b>\n"
            f"👫 Referal: <b>{max(0, int(referral_count))} ta</b>\n"
            f"📈 Olingan: <b>{_format_balance(lifetime_tokens_earned)} token</b>\n"
            f"📉 Sarflangan: <b>{_format_balance(lifetime_tokens_spent)} token</b>\n\n"
            "Referral markazi, balans va tariflar uchun tugmalardan foydalaning."
        )
        if str(user_plan or "").strip().lower() == "free":
            reset_label = str(free_reset_date or "").replace("T", " ")[:16]
            if free_reset_tokens > 0 and free_reset_hours > 0:
                text += (
                    f"\n\n🔄 Free refill: <b>{int(free_reset_tokens)} token / "
                    f"{int(free_reset_hours)} soat</b>"
                )
            if reset_label:
                text += f"\n⏱ Keyingi refill: <b>{reset_label}</b>"
        else:
            plan_reset_label = str(reset_date or "").replace("T", " ")[:16]
            if plan_reset_label:
                text += f"\n⏱ Keyingi plan reset: <b>{plan_reset_label}</b>"
        if referrer_id > 0:
            text += f"\n\n👥 Sizni taklif qilgan ID: <code>{int(referrer_id)}</code>"
        if referral_inviter_bonus > 0 or referral_invitee_bonus > 0:
            text += (
                "\n\n🎁 Referal bonuslari:"
                f"\n- Do'stingizga: <b>{int(referral_invitee_bonus)}</b> token"
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
        f"👫 Taklif qilgan do'stlaringiz: <b>{max(0, int(referral_count))} ta</b>",
    ]
    if referrer_id > 0:
        rows.append(f"👥 Sizni taklif qilgan ID: <code>{int(referrer_id)}</code>")
    if referral_inviter_bonus > 0 or referral_invitee_bonus > 0:
        rows.append("")
        rows.append("🎁 Referal bonuslari:")
        rows.append(f"- Do'stingizga: <b>{int(referral_invitee_bonus)}</b> token")
        rows.append(f"- Sizga: <b>{int(referral_inviter_bonus)}</b> token")
    if referral_link:
        rows.append("")
        rows.append(f"🔗 Referal link: <code>{referral_link}</code>")
    if free_reset_tokens > 0 and free_reset_hours > 0:
        rows.append("")
        rows.append(
            f"🔄 Free refill: <b>{int(free_reset_tokens)} token / {int(free_reset_hours)} soat</b>"
        )
    refill_label = str(free_reset_date or "").replace("T", " ")[:16]
    if refill_label:
        rows.append(f"⏱ Keyingi refill: <b>{refill_label}</b>")
    rows.append("")
    rows.append("Do'stingiz botga sizning linkingiz orqali kirsa, ikkalangiz ham bonus olasiz.")
    return "\n".join(rows)


def referral_keyboard(referral_link: str = "") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if referral_link.startswith("https://t.me/"):
        rows.append([InlineKeyboardButton(text="🔗 Linkni ochish", url=referral_link)])
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
