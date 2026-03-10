from __future__ import annotations

import html
from typing import Any

from aiogram.types import CallbackQuery, Message

from services.ai_store import AIStore
from services.token_pricing import (
    ServiceTariff,
    resolve_service_key,
    service_cost,
    service_tariff,
)

COMPLIMENTARY_SERVICE_KEYS = {
    "youtube_download_video",
    "youtube_download_audio",
    "social_download",
}


def is_complimentary_service(service_key: str) -> bool:
    return resolve_service_key(service_key) in COMPLIMENTARY_SERVICE_KEYS


def event_identity(event: Message | CallbackQuery) -> tuple[int, str, str]:
    user = event.from_user
    if user is None:
        return 0, "", ""
    full_name = " ".join(
        part
        for part in [
            str(getattr(user, "first_name", "") or "").strip(),
            str(getattr(user, "last_name", "") or "").strip(),
        ]
        if part
    ).strip()
    return int(user.id), str(getattr(user, "username", "") or "").strip(), full_name


async def preview_charge(
    ai_store: AIStore,
    event: Message | CallbackQuery,
    service_key: str,
    *,
    custom_cost: int | None = None,
) -> tuple[dict[str, Any], ServiceTariff, int, int, str, str]:
    user_id, username, full_name = event_identity(event)
    user = await ai_store.ensure_user(
        user_id=user_id,
        username=username,
        full_name=full_name,
    )
    cost = (
        int(custom_cost)
        if isinstance(custom_cost, int) and custom_cost > 0
        else service_cost(service_key, plan=str(user.get("current_plan", "free") or "free"))
    )
    effective_key = resolve_service_key(service_key)
    if cost > 0 and is_complimentary_service(effective_key):
        if await ai_store.can_use_complimentary_service(
            user_id=user_id,
            username=username,
            full_name=full_name,
            service_key=effective_key,
            user=user,
        ):
            cost = 0
    tariff = service_tariff(service_key)
    return user, tariff, cost, user_id, username, full_name


def insufficient_balance_text(*, label: str, required: int, balance: int) -> str:
    return (
        "<b>Token yetarli emas</b>\n"
        f"Xizmat: <b>{html.escape(label)}</b>\n"
        f"Kerak: <b>{int(required)}</b> token\n"
        f"Balans: <b>{int(balance)}</b> token\n\n"
        "Balansni referral yoki Premium orqali oshiring."
    )


async def ensure_balance(
    ai_store: AIStore,
    event: Message | CallbackQuery,
    service_key: str,
    *,
    custom_cost: int | None = None,
    reply_markup: Any = None,
) -> tuple[dict[str, Any], int, int, str, str] | None:
    user, tariff, cost, user_id, username, full_name = await preview_charge(
        ai_store,
        event,
        service_key,
        custom_cost=custom_cost,
    )
    balance = int(user.get("token_balance", 0) or 0)
    if balance >= cost:
        return user, cost, user_id, username, full_name

    text = insufficient_balance_text(
        label=tariff.label,
        required=cost,
        balance=balance,
    )
    if isinstance(event, CallbackQuery):
        await event.answer(f"{tariff.label}: {cost} token kerak", show_alert=True)
        if event.message is not None:
            await event.message.answer(
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
    else:
        await event.answer(
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    return None


async def finalize_charge(
    ai_store: AIStore,
    *,
    service_key: str,
    user_id: int,
    username: str,
    full_name: str,
    amount: int,
) -> dict[str, Any]:
    token_amount = max(0, int(amount))
    if token_amount > 0:
        return await ai_store.charge_tokens(
            user_id=user_id,
            username=username,
            full_name=full_name,
            amount=token_amount,
        )
    if is_complimentary_service(service_key):
        return await ai_store.consume_complimentary_service(
            user_id=user_id,
            username=username,
            full_name=full_name,
            service_key=service_key,
        )
    return await ai_store.ensure_user(
        user_id=user_id,
        username=username,
        full_name=full_name,
    )
