from html import unescape
import os
from typing import Any
from urllib.parse import quote

import aiohttp

WIKI_API_TEMPLATE = "https://{lang}.wikipedia.org/w/api.php"
HTTP_TIMEOUT_SECONDS = 15
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (XizmatlarBot/1.0; +https://core.telegram.org/bots/api)"
)


async def _request_json(lang: str, params: dict[str, str | int]) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    url = WIKI_API_TEMPLATE.format(lang=lang)
    request_params: dict[str, str | int] = {
        "format": "json",
        "formatversion": 2,
    }
    request_params.update(params)

    headers = {
        "User-Agent": os.getenv("HTTP_USER_AGENT", "").strip() or DEFAULT_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, params=request_params) as response:
            if response.status >= 500:
                raise RuntimeError("Wikipedia xizmati vaqtincha ishlamayapti.")
            if response.status >= 400:
                body = (await response.text())[:180]
                raise RuntimeError(
                    f"Wikipedia API xatosi: HTTP {response.status}. {body}"
                )
            response.raise_for_status()
            payload = await response.json(content_type=None)

    if not isinstance(payload, dict):
        raise RuntimeError("Wikipedia API notogri javob qaytardi.")
    return payload


async def _search_title(query: str, lang: str) -> str:
    payload = await _request_json(
        lang,
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": 1,
            "utf8": 1,
        },
    )
    query_data = payload.get("query")
    if not isinstance(query_data, dict):
        return ""
    search = query_data.get("search")
    if not isinstance(search, list) or not search:
        return ""
    first = search[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("title", "")).strip()


async def _page_intro(title: str, lang: str) -> str:
    payload = await _request_json(
        lang,
        {
            "action": "query",
            "prop": "extracts",
            "titles": title,
            "exintro": 1,
            "explaintext": 1,
            "redirects": 1,
            "utf8": 1,
        },
    )
    query_data = payload.get("query")
    if not isinstance(query_data, dict):
        return ""
    pages = query_data.get("pages")
    if not isinstance(pages, list) or not pages:
        return ""
    first = pages[0]
    if not isinstance(first, dict):
        return ""
    return str(first.get("extract", "")).strip()


async def search_wikipedia_summary(
    query: str, preferred_lang: str = "uz"
) -> dict[str, str]:
    clean_query = (query or "").strip()
    if not clean_query:
        raise ValueError("Wikipedia uchun qidiruv so'zini yuboring.")

    languages = [preferred_lang]
    if preferred_lang != "en":
        languages.append("en")

    for lang in languages:
        title = await _search_title(clean_query, lang=lang)
        if not title:
            continue
        intro = await _page_intro(title, lang=lang)
        summary = unescape(intro).strip() or "Ushbu maqola uchun qisqa tavsif topilmadi."
        safe_title = title.replace(" ", "_")
        page_url = f"https://{lang}.wikipedia.org/wiki/{quote(safe_title)}"
        return {
            "title": title,
            "summary": summary,
            "url": page_url,
            "lang": lang,
        }

    raise ValueError("Maqola topilmadi. Boshqa so'z bilan qayta urinib ko'ring.")
