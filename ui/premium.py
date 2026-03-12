from __future__ import annotations

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from services.token_pricing import premium_card_number


def upgrade_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Premium", callback_data="premium:page")]
        ]
    )


def premium_page_keyboard(
    *,
    is_active: bool,
    has_pending_request: bool,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="Kartani nusxalash",
                copy_text=CopyTextButton(text=premium_card_number()),
            )
        ]
    ]
    if is_active:
        rows.append(
            [InlineKeyboardButton(text="Kabinet", callback_data="menu:section:cabinet")]
        )
    elif has_pending_request:
        rows.append(
            [InlineKeyboardButton(text="Holatni yangilash", callback_data="premium:page")]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="To'lov qildim", callback_data="premium:buy")]
        )
    rows.append([InlineKeyboardButton(text="Orqaga", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def premium_upload_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Kartani nusxalash",
                    copy_text=CopyTextButton(text=premium_card_number()),
                )
            ],
            [InlineKeyboardButton(text="Bekor qilish", callback_data="premium:page")],
        ]
    )


def premium_admin_request_keyboard(
    *,
    request_id: int,
    contact_url: str,
    processed: bool = False,
) -> InlineKeyboardMarkup:
    if processed:
        rows = [[InlineKeyboardButton(text="Yangilash", callback_data="admin:premium")]]
    else:
        rows = [
            [
                InlineKeyboardButton(
                    text="Tasdiqlash",
                    callback_data=f"premium:approve:{int(request_id)}",
                ),
                InlineKeyboardButton(
                    text="Rad etish",
                    callback_data=f"premium:reject:{int(request_id)}",
                ),
            ]
        ]
    if contact_url:
        rows.append([InlineKeyboardButton(text="Foydalanuvchi bilan chat", url=contact_url)])
    rows.append([InlineKeyboardButton(text="Admin panel", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def premium_admin_list_keyboard(requests: list[dict[str, object]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in requests[:10]:
        request_id = int(item.get("request_id", 0) or 0)
        user_id = int(item.get("user_id", 0) or 0)
        username = str(item.get("username", "") or "").strip()
        label = f"#{request_id} - {user_id}"
        if username:
            label = f"#{request_id} - @{username}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"admin:premium:item:{request_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Admin panel", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
