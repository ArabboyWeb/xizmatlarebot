import os
import random
import string
from dataclasses import dataclass
from typing import Any

import aiohttp

DEFAULT_API_BASE = "https://www.1secmail.com/api/v1/"
HTTP_TIMEOUT_SECONDS = 15
FALLBACK_DOMAINS = (
    "1secmail.com",
    "1secmail.org",
    "1secmail.net",
    "wwjmp.com",
    "esiix.com",
    "xojxe.com",
    "yoggm.com",
)


@dataclass(slots=True)
class TempMailMessagePreview:
    message_id: int
    from_email: str
    subject: str
    date: str


def _normalize_base(api_base: str | None) -> str:
    raw = (api_base or os.getenv("ONESECMAIL_API_BASE", DEFAULT_API_BASE)).strip()
    if not raw:
        raw = DEFAULT_API_BASE
    return raw if raw.endswith("/") else f"{raw}/"


def split_mailbox(email: str) -> tuple[str, str]:
    value = (email or "").strip().lower()
    if "@" not in value:
        raise ValueError("Email format notogri.")
    login, domain = value.split("@", maxsplit=1)
    login = login.strip()
    domain = domain.strip()
    if not login or not domain:
        raise ValueError("Email format notogri.")
    return login, domain


async def _request_json(
    action: str, params: dict[str, str | int], api_base: str | None = None
) -> Any:
    base = _normalize_base(api_base)
    query: dict[str, str | int] = {"action": action}
    query.update(params)

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(base, params=query) as response:
            if response.status >= 500:
                raise RuntimeError("1secmail xizmati vaqtincha ishlamayapti.")
            if response.status >= 400:
                body = (await response.text())[:120].strip()
                raise RuntimeError(
                    f"1secmail xatosi: HTTP {response.status}. {body or 'No body'}"
                )
            payload = await response.json(content_type=None)
    return payload


async def get_domains(api_base: str | None = None) -> list[str]:
    try:
        payload = await _request_json("getDomainList", {}, api_base=api_base)
    except Exception:
        return list(FALLBACK_DOMAINS)

    if not isinstance(payload, list):
        return list(FALLBACK_DOMAINS)

    domains: list[str] = []
    for item in payload:
        value = str(item).strip().lower()
        if value and "." in value:
            domains.append(value)
    return domains or list(FALLBACK_DOMAINS)


async def create_mailbox(api_base: str | None = None) -> str:
    try:
        payload = await _request_json(
            "genRandomMailbox",
            {"count": 1},
            api_base=api_base,
        )
        if isinstance(payload, list) and payload:
            candidate = str(payload[0]).strip().lower()
            split_mailbox(candidate)
            return candidate
    except Exception:
        # Graceful fallback for temporary API issues in mailbox generation.
        pass

    domains = await get_domains(api_base=api_base)
    login = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"{login}@{random.choice(domains)}"


async def fetch_inbox(
    email: str, api_base: str | None = None
) -> list[TempMailMessagePreview]:
    login, domain = split_mailbox(email)
    payload = await _request_json(
        "getMessages",
        {"login": login, "domain": domain},
        api_base=api_base,
    )
    if not isinstance(payload, list):
        raise RuntimeError("Inbox javobi notogri formatda keldi.")

    messages: list[TempMailMessagePreview] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            message_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        messages.append(
            TempMailMessagePreview(
                message_id=message_id,
                from_email=str(item.get("from", "")).strip() or "Nomalum",
                subject=str(item.get("subject", "")).strip() or "(No subject)",
                date=str(item.get("date", "")).strip() or "Nomalum",
            )
        )
    return messages


async def read_message(
    email: str, message_id: int, api_base: str | None = None
) -> dict[str, Any]:
    login, domain = split_mailbox(email)
    payload = await _request_json(
        "readMessage",
        {"login": login, "domain": domain, "id": int(message_id)},
        api_base=api_base,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Xabar oqishda API notogri javob qaytardi.")
    return payload
