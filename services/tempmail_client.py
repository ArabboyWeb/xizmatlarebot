import os
import random
import string
from dataclasses import dataclass
from typing import Any

import aiohttp

from services.load_control import run_with_limit

DEFAULT_API_BASE = "https://www.1secmail.com/api/v1/"
MAILTM_API_BASE = "https://api.mail.tm"
HTTP_TIMEOUT_SECONDS = 18
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (XizmatlarBot/1.0; +https://core.telegram.org/bots/api)"
)
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
    message_id: str
    from_email: str
    subject: str
    date: str


@dataclass(slots=True)
class MailTmAccount:
    address: str
    password: str
    token: str


_MAILTM_CACHE: dict[str, MailTmAccount] = {}


def _headers() -> dict[str, str]:
    return {
        "User-Agent": os.getenv("HTTP_USER_AGENT", "").strip() or DEFAULT_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    }


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
    async def _run() -> Any:
        base = _normalize_base(api_base)
        query: dict[str, str | int] = {"action": action}
        query.update(params)

        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout, headers=_headers()) as session:
            async with session.get(base, params=query) as response:
                if response.status >= 500:
                    raise RuntimeError("1secmail xizmati vaqtincha ishlamayapti.")
                if response.status >= 400:
                    body = (await response.text())[:160].strip()
                    raise RuntimeError(
                        f"1secmail xatosi: HTTP {response.status}. {body or 'No body'}"
                    )
                return await response.json(content_type=None)

    return await run_with_limit("api", _run)


async def _mailtm_request_json(
    method: str,
    path: str,
    *,
    token: str = "",
    params: dict[str, str | int] | None = None,
    json_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        base = os.getenv("MAILTM_API_BASE", MAILTM_API_BASE).strip() or MAILTM_API_BASE
        base = base.rstrip("/")
        url = f"{base}{path}"
        headers = _headers()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.request(
                method,
                url,
                params=params,
                json=json_data,
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    message = ""
                    if isinstance(body, dict):
                        message = str(body.get("detail", "")).strip()
                    raise RuntimeError(
                        f"mail.tm xatosi: HTTP {response.status}. {message or 'Request failed'}"
                    )

        if not isinstance(body, dict):
            raise RuntimeError("mail.tm noto'g'ri javob qaytardi.")
        return body

    return await run_with_limit("api", _run)


async def _mailtm_create_account() -> MailTmAccount:
    domains_payload = await _mailtm_request_json("GET", "/domains", params={"page": 1})
    members = domains_payload.get("hydra:member")
    if not isinstance(members, list) or not members:
        raise RuntimeError("mail.tm domenlari topilmadi.")

    domain = ""
    for item in members:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("domain", "")).strip().lower()
        if candidate:
            domain = candidate
            break
    if not domain:
        raise RuntimeError("mail.tm domeni olinmadi.")

    login = "xizmat" + "".join(
        random.choices(string.ascii_lowercase + string.digits, k=8)
    )
    address = f"{login}@{domain}"
    password = "Pwd" + "".join(
        random.choices(string.ascii_letters + string.digits, k=16)
    )

    await _mailtm_request_json(
        "POST",
        "/accounts",
        json_data={"address": address, "password": password},
    )
    token_payload = await _mailtm_request_json(
        "POST",
        "/token",
        json_data={"address": address, "password": password},
    )
    token = str(token_payload.get("token", "")).strip()
    if not token:
        raise RuntimeError("mail.tm token olinmadi.")

    account = MailTmAccount(address=address, password=password, token=token)
    _MAILTM_CACHE[address] = account
    return account


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
        pass

    account = await _mailtm_create_account()
    return account.address


async def _fetch_mailtm_inbox(email: str) -> list[TempMailMessagePreview]:
    account = _MAILTM_CACHE.get(email)
    if account is None:
        raise RuntimeError(
            "mail.tm akkaunt topilmadi. Yangi email yaratib qayta urinib ko'ring."
        )

    payload = await _mailtm_request_json("GET", "/messages", token=account.token)
    members = payload.get("hydra:member")
    if not isinstance(members, list):
        return []

    messages: list[TempMailMessagePreview] = []
    for item in members:
        if not isinstance(item, dict):
            continue
        msg_id = str(item.get("id", "")).strip()
        if not msg_id:
            continue
        from_data = item.get("from")
        from_email = ""
        if isinstance(from_data, dict):
            from_email = str(from_data.get("address", "")).strip()
        messages.append(
            TempMailMessagePreview(
                message_id=msg_id,
                from_email=from_email or "Nomalum",
                subject=str(item.get("subject", "")).strip() or "(No subject)",
                date=str(item.get("createdAt", "")).strip() or "Nomalum",
            )
        )
    return messages


async def fetch_inbox(
    email: str, api_base: str | None = None
) -> list[TempMailMessagePreview]:
    normalized = (email or "").strip().lower()
    split_mailbox(normalized)

    if normalized in _MAILTM_CACHE:
        return await _fetch_mailtm_inbox(normalized)

    try:
        login, domain = split_mailbox(normalized)
        payload = await _request_json(
            "getMessages",
            {"login": login, "domain": domain},
            api_base=api_base,
        )
        if not isinstance(payload, list):
            raise RuntimeError("Inbox javobi notogri formatda keldi.")
    except Exception:
        if normalized in _MAILTM_CACHE:
            return await _fetch_mailtm_inbox(normalized)
        raise

    messages: list[TempMailMessagePreview] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        message_id_raw = item.get("id")
        message_id = str(message_id_raw).strip()
        if not message_id:
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


async def _read_mailtm_message(email: str, message_id: str) -> dict[str, Any]:
    account = _MAILTM_CACHE.get(email)
    if account is None:
        raise RuntimeError(
            "mail.tm akkaunt topilmadi. Yangi email yaratib qayta urinib ko'ring."
        )
    payload = await _mailtm_request_json(
        "GET", f"/messages/{message_id}", token=account.token
    )
    from_data = payload.get("from")
    from_email = ""
    if isinstance(from_data, dict):
        from_email = str(from_data.get("address", "")).strip()

    text_body = str(payload.get("text", "")).strip()
    html_body = str(payload.get("html", "")).strip()
    attachments: list[dict[str, Any]] = []
    raw_attachments = payload.get("attachments")
    if isinstance(raw_attachments, list):
        for item in raw_attachments:
            if not isinstance(item, dict):
                continue
            attachments.append(
                {
                    "filename": str(item.get("filename", "file")).strip(),
                    "size": str(item.get("size", "")).strip(),
                }
            )

    return {
        "id": message_id,
        "from": from_email or "Nomalum",
        "subject": str(payload.get("subject", "")).strip() or "(No subject)",
        "date": str(payload.get("createdAt", "")).strip() or "Nomalum",
        "textBody": text_body,
        "htmlBody": html_body,
        "attachments": attachments,
    }


async def read_message(
    email: str, message_id: str | int, api_base: str | None = None
) -> dict[str, Any]:
    normalized = (email or "").strip().lower()
    split_mailbox(normalized)
    clean_message_id = str(message_id).strip()
    if not clean_message_id:
        raise ValueError("Message ID bo'sh bo'lishi mumkin emas.")

    if normalized in _MAILTM_CACHE:
        return await _read_mailtm_message(normalized, clean_message_id)

    login, domain = split_mailbox(normalized)
    payload = await _request_json(
        "readMessage",
        {"login": login, "domain": domain, "id": clean_message_id},
        api_base=api_base,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Xabar oqishda API notogri javob qaytardi.")
    return payload
