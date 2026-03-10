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
        [
            InlineKeyboardButton(
                text="💎 Sun'iy Intellekt", callback_data="menu:section:ai"
            )
        ],
        [
            InlineKeyboardButton(
                text="📥 Media & Yuklab olish", callback_data="menu:section:media"
            ),
            InlineKeyboardButton(
                text="🛠 Foydali Asboblar", callback_data="menu:section:tools"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔍 Ma'lumot qidirish", callback_data="menu:section:search"
            ),
            InlineKeyboardButton(
                text="👤 Kabinet / Balans", callback_data="menu:section:cabinet"
            ),
        ],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="Admin panel", callback_data="admin:panel")])
    return rows


def _section_rows(section: MenuSection) -> list[list[InlineKeyboardButton]]:
    if section == "ai":
        return [
            [
                InlineKeyboardButton(text="💬 AI Chat", callback_data="services:ai"),
                InlineKeyboardButton(
                    text="🎨 Rasm Yaratish", callback_data="services:pollinations"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📄 PDF/Doc Konvertor", callback_data="services:converter"
                ),
                InlineKeyboardButton(
                    text="💳 Premiumga o'tish", callback_data="ai:plans"
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:main")],
        ]
    if section == "media":
        return [
            [
                InlineKeyboardButton(
                    text="🎥 YouTube Video/Audio", callback_data="services:youtube"
                ),
                InlineKeyboardButton(
                    text="🎵 Musiqa qidirish", callback_data="services:shazam"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💾 Fayllarni saqlash", callback_data="services:save"
                ),
                InlineKeyboardButton(
                    text="🖼 Instagram/TikTok Saver", callback_data="services:save"
                )
            ],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:main")],
        ]
    if section == "tools":
        return [
            [
                InlineKeyboardButton(
                    text="💱 Valyuta kursi", callback_data="services:currency"
                ),
                InlineKeyboardButton(
                    text="☁️ Ob-havo", callback_data="services:weather"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🌐 Tarjimon", callback_data="services:translate"
                ),
                InlineKeyboardButton(
                    text="🔗 Link qisqartirish", callback_data="services:tinyurl"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📄 Konvertor", callback_data="services:converter"
                )
            ],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:main")],
        ]
    if section == "search":
        return [
            [
                InlineKeyboardButton(
                    text="💼 Ish qidirish", callback_data="services:jobs"
                ),
                InlineKeyboardButton(
                    text="📖 Wikipedia", callback_data="services:wikipedia"
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
        return [
            [
                InlineKeyboardButton(
                    text="💳 Balans tafsiloti", callback_data="services:ai"
                )
            ],
            [
                InlineKeyboardButton(text="💬 AI Chat", callback_data="services:ai"),
                InlineKeyboardButton(
                    text="⭐ Premium tariflar", callback_data="ai:plans"
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="menu:main")],
        ]
    return _main_rows(False)


def services_keyboard(
    is_admin: bool = False,
    *,
    section: MenuSection = "main",
) -> InlineKeyboardMarkup:
    rows = _main_rows(is_admin) if section == "main" else _section_rows(section)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_text(
    upload_limit_bytes: int,
    download_limit_bytes: int,
    *,
    user_plan: str = "free",
    token_balance: int = 0,
    referral_count: int = 0,
    referral_link: str = "",
    notice: str = "",
    is_admin: bool = False,
) -> str:
    _ = upload_limit_bytes, download_limit_bytes, is_admin
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
    if referral_link:
        text += f"\n\n🔗 Referal linkingiz: <code>{referral_link}</code>"
    return text


def section_menu_text(
    section: MenuSection,
    *,
    user_plan: str = "free",
    token_balance: int = 0,
    referral_count: int = 0,
    referral_link: str = "",
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
            "🎥 YouTube video/audio\n"
            "🎵 Musiqa qidirish\n"
            "💾 Fayllarni saqlash\n"
            "🖼 Instagram/TikTok saver"
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
            f"👫 Referal: <b>{max(0, int(referral_count))} ta</b>\n\n"
            "Balans tafsiloti va Premium tariflar uchun tugmalardan foydalaning."
        )
        if referral_link:
            text += f"\n\n🔗 Referal link: <code>{referral_link}</code>"
        return text
    return ""


async def safe_edit_menu(
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
            logger.warning("Menu edit failed: %s", error)
