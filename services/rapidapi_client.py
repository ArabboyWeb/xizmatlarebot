import os
from typing import Any

import aiohttp

from services.load_control import run_with_limit

HTTP_TIMEOUT_SECONDS = 25


def _rapidapi_key() -> str:
    key = os.getenv("RAPIDAPI_KEY", "").strip()
    if not key:
        raise RuntimeError("RAPIDAPI_KEY topilmadi. .env faylga kalit qo'shing.")
    return key


def _headers(host: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "x-rapidapi-key": _rapidapi_key(),
        "x-rapidapi-host": host,
        "Accept": "application/json, text/plain, */*",
    }
    if extra:
        headers.update(extra)
    return headers


def _extract_error_message(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                base = value.strip()
                if "not subscribed" in base.lower():
                    return (
                        f"{base} RapidAPI dashboard orqali ushbu API ga "
                        "free plan bo'lsa subscribe qiling."
                    )
                return base
    return f"RapidAPI xatosi: HTTP {status}"


async def rapidapi_get(
    *,
    host: str,
    url: str,
    params: dict[str, str | int] | None = None,
) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                params=params,
                headers=_headers(host),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(_extract_error_message(payload, response.status))

        if not isinstance(payload, dict):
            raise RuntimeError("RapidAPI noto'g'ri formatda javob qaytardi.")
        return payload

    return await run_with_limit("api", _run)


async def rapidapi_post_form(
    *,
    host: str,
    url: str,
    data: dict[str, str],
) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        headers = _headers(
            host,
            extra={
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                data=data,
                headers=headers,
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(_extract_error_message(payload, response.status))

        if not isinstance(payload, dict):
            raise RuntimeError("RapidAPI noto'g'ri formatda javob qaytardi.")
        return payload

    return await run_with_limit("api", _run)


async def rapidapi_post_json(
    *,
    host: str,
    url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        headers = _headers(
            host,
            extra={
                "Content-Type": "application/json",
            },
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                json=payload,
                headers=headers,
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(_extract_error_message(body, response.status))

        if not isinstance(body, dict):
            raise RuntimeError("RapidAPI noto'g'ri formatda javob qaytardi.")
        return body

    return await run_with_limit("api", _run)
