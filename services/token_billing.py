from __future__ import annotations

import html
from typing import Any

from aiogram.types import CallbackQuery, Message

from services.ai_store import AIStore
from services.group_command_mode import is_group_chat
from services.token_pricing import (
    ServiceTariff,
    premium_monthly_credits,
    premium_upgrade_tokens,
    resolve_service_key,
    service_cost,
    service_daily_limit,
    service_tariff,
)
from ui.premium import upgrade_prompt_keyboard

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
        "<b>Kredit yetarli emas</b>\n"
        f"Xizmat: <b>{html.escape(label)}</b>\n"
        f"Kerak: <b>{int(required)}</b> kredit\n"
        f"Balans: <b>{int(balance)}</b> kredit\n\n"
        "Premium bilan balansni tezroq oshiring:\n"
        f"- har oy {premium_monthly_credits()} kredit"
    )


def quota_limit_text(*, label: str, limit: int) -> str:
    return (
        "<b>Kunlik limit tugadi</b>\n"
        f"Xizmat: <b>{html.escape(label)}</b>\n"
        f"Bugungi limit: <b>{int(limit)}</b> ta"
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
    quota = await ai_store.can_use_service_quota(
        user_id=user_id,
        username=username,
        full_name=full_name,
        service_key=service_key,
    )
    if bool(quota.get("tracked")):
        if not bool(quota.get("allowed")):
            text = quota_limit_text(
                label=tariff.label,
                limit=int(quota.get("limit", 0) or 0),
            )
            if isinstance(event, CallbackQuery):
                await event.answer("Kunlik limit tugadi.", show_alert=True)
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
        cost = 0
    balance = int(user.get("token_balance", 0) or 0)
    if balance >= cost:
        return user, cost, user_id, username, full_name

    text = insufficient_balance_text(
        label=tariff.label,
        required=cost,
        balance=balance,
    )
    private_upgrade_prompt = not is_group_chat(event)
    target_reply_markup = (
        upgrade_prompt_keyboard() if private_upgrade_prompt else reply_markup
    )
    if isinstance(event, CallbackQuery):
        if private_upgrade_prompt:
            await event.answer(
                (
                    "Kredit yetarli emas.\n"
                    f"Premium bilan har oy {premium_monthly_credits()} kredit oling."
                ),
                show_alert=True,
            )
        else:
            await event.answer(f"{tariff.label}: {cost} kredit kerak", show_alert=True)
        if event.message is not None:
            await event.message.answer(
                text,
                parse_mode="HTML",
                reply_markup=target_reply_markup,
            )
    else:
        await event.answer(
            text,
            parse_mode="HTML",
            reply_markup=target_reply_markup,
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
            service_key=service_key,
        )
    quota_limit = service_daily_limit(
        service_key,
        plan=str(
            (
                await ai_store.ensure_user(
                    user_id=user_id,
                    username=username,
                    full_name=full_name,
                )
            ).get("current_plan", "free")
            or "free"
        ),
    )
    if quota_limit is not None:
        return await ai_store.consume_service_quota(
            user_id=user_id,
            username=username,
            full_name=full_name,
            service_key=service_key,
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
