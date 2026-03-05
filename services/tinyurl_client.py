import os
from urllib.parse import urlparse

import aiohttp

TINYURL_CREATE_URL = "https://api.tinyurl.com/create"
TINYURL_LEGACY_URL = "https://tinyurl.com/api-create.php"
HTTP_TIMEOUT_SECONDS = 15


def _validate_url(url: str) -> str:
    value = (url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Faqat toliq http/https URL yuboring.")
    return value


async def _shorten_legacy(url: str) -> str:
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(TINYURL_LEGACY_URL, params={"url": url}) as response:
            response.raise_for_status()
            body = (await response.text()).strip()

    if body.startswith("http://") or body.startswith("https://"):
        return body
    raise RuntimeError("TinyURL legacy endpoint yaroqli javob qaytarmadi.")


async def _shorten_official(url: str, api_token: str, domain: str) -> str:
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"url": url, "domain": domain}

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            TINYURL_CREATE_URL, headers=headers, json=payload
        ) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                error_text = ""
                if isinstance(body, dict):
                    errors = body.get("errors")
                    if isinstance(errors, list) and errors:
                        error_text = str(errors[0])
                raise RuntimeError(
                    f"TinyURL API xatosi: HTTP {response.status}. {error_text or 'Request failed'}"
                )

    if not isinstance(body, dict):
        raise RuntimeError("TinyURL API notogri formatda javob qaytardi.")
    data = body.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("TinyURL API javobida data yoq.")
    tiny = str(data.get("tiny_url", "")).strip()
    if not tiny:
        raise RuntimeError("Qisqa URL olinmadi.")
    return tiny


async def shorten_url(url: str) -> tuple[str, str]:
    clean_url = _validate_url(url)
    api_token = os.getenv("TINYURL_API_TOKEN", "").strip()
    domain = os.getenv("TINYURL_DOMAIN", "tinyurl.com").strip() or "tinyurl.com"

    if api_token:
        short = await _shorten_official(clean_url, api_token=api_token, domain=domain)
        return short, "official"

    short = await _shorten_legacy(clean_url)
    return short, "legacy"
