import html
import re
from typing import Any

import aiohttp

from services.rapidapi_client import HTTP_TIMEOUT_SECONDS, rapidapi_post_json

YOUTUBE_RAPID_HOST = "youtube-search-and-download.p.rapidapi.com"
YOUTUBE_CHANNEL_SEARCH_URL = (
    "https://youtube-search-and-download.p.rapidapi.com/channel/search"
)
YOUTUBE_CHANNEL_SEARCH_FALLBACK_URL = "https://www.youtube.com/channel/{channel_id}/search"
VIDEO_ID_RE = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')
TITLE_RE = re.compile(r'"title":\{"runs":\[\{"text":"(.*?)"\}\]')
PUBLISHED_RE = re.compile(r'"publishedTimeText":\{"simpleText":"(.*?)"\}')
DURATION_RE = re.compile(r'"lengthText":\{.*?"simpleText":"(.*?)"\}', re.S)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "simpleText", "title"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
        runs = value.get("runs")
        if isinstance(runs, list):
            chunks: list[str] = []
            for item in runs:
                if isinstance(item, dict):
                    txt = str(item.get("text", "")).strip()
                    if txt:
                        chunks.append(txt)
            if chunks:
                return "".join(chunks).strip()
    return ""


def _flatten_dicts(node: Any) -> list[dict[str, Any]]:
    stack = [node]
    found: list[dict[str, Any]] = []
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            found.append(current)
            for value in current.values():
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _extract_videos(payload: dict[str, Any]) -> list[dict[str, str]]:
    videos: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for item in _flatten_dicts(payload):
        video_id = str(item.get("videoId", "")).strip()
        if not video_id or video_id in seen_ids:
            continue
        title = ""
        for key in ("title", "headline", "name"):
            if key in item:
                title = _as_text(item.get(key))
                if title:
                    break
        if not title:
            title = f"Video {video_id}"

        duration = _as_text(item.get("lengthText")) or _as_text(item.get("length"))
        published = _as_text(item.get("publishedTimeText")) or _as_text(
            item.get("published")
        )
        videos.append(
            {
                "video_id": video_id,
                "title": title,
                "duration": duration,
                "published": published,
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
        seen_ids.add(video_id)
        if len(videos) >= 10:
            break
    return videos


def _decode_fragment(value: str) -> str:
    text = value.encode("utf-8").decode("unicode_escape", errors="ignore")
    return html.unescape(text).strip()


def _extract_html_videos(page_html: str) -> list[dict[str, str]]:
    videos: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for match in VIDEO_ID_RE.finditer(page_html):
        video_id = match.group(1)
        if video_id in seen_ids:
            continue
        snippet = page_html[match.start() : match.start() + 3500]
        title_match = TITLE_RE.search(snippet)
        if not title_match:
            continue
        published_match = PUBLISHED_RE.search(snippet)
        duration_match = DURATION_RE.search(snippet)
        videos.append(
            {
                "video_id": video_id,
                "title": _decode_fragment(title_match.group(1)) or f"Video {video_id}",
                "duration": _decode_fragment(duration_match.group(1))
                if duration_match
                else "",
                "published": _decode_fragment(published_match.group(1))
                if published_match
                else "",
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
        seen_ids.add(video_id)
        if len(videos) >= 10:
            break
    return videos


async def _fallback_channel_search(channel_id: str, query: str) -> list[dict[str, str]]:
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": "Mozilla/5.0 (XizmatlarBot/1.0)",
    }
    url = YOUTUBE_CHANNEL_SEARCH_FALLBACK_URL.format(channel_id=channel_id)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(url, params={"query": query}) as response:
            page_html = await response.text()
            if response.status >= 400:
                raise RuntimeError(
                    f"YouTube HTML fallback xatosi: HTTP {response.status}"
                )

    videos = _extract_html_videos(page_html)
    if not videos:
        raise RuntimeError("YouTube HTML fallback natija topmadi.")
    return videos


async def search_channel_videos(
    channel_id: str,
    query: str,
    *,
    next_token: str = "",
) -> dict[str, Any]:
    clean_channel_id = (channel_id or "").strip()
    clean_query = (query or "").strip()
    if not clean_channel_id:
        raise ValueError("Channel ID yuborish kerak.")
    if not clean_query:
        raise ValueError("YouTube qidiruvi uchun so'rov yuboring.")

    payload_data: dict[str, Any] = {"id": clean_channel_id, "query": clean_query}
    if next_token.strip():
        payload_data["next"] = next_token.strip()

    rapidapi_error: Exception | None = None
    try:
        payload = await rapidapi_post_json(
            host=YOUTUBE_RAPID_HOST,
            url=YOUTUBE_CHANNEL_SEARCH_URL,
            payload=payload_data,
        )
        videos = _extract_videos(payload)
        next_value = str(payload.get("next", "")).strip()
        if videos:
            return {
                "channel_id": clean_channel_id,
                "query": clean_query,
                "videos": videos,
                "next": next_value,
            }
    except Exception as error:  # noqa: BLE001
        rapidapi_error = error

    try:
        videos = await _fallback_channel_search(clean_channel_id, clean_query)
    except Exception as fallback_error:  # noqa: BLE001
        if rapidapi_error is not None:
            raise RuntimeError(
                f"YouTube qidiruvi ishlamadi. RapidAPI: {rapidapi_error}. "
                f"Fallback: {fallback_error}."
            ) from fallback_error
        raise

    return {
        "channel_id": clean_channel_id,
        "query": clean_query,
        "videos": videos,
        "next": "",
    }
