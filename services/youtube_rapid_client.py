from typing import Any

from services.rapidapi_client import rapidapi_post_json

YOUTUBE_RAPID_HOST = "youtube-search-and-download.p.rapidapi.com"
YOUTUBE_CHANNEL_SEARCH_URL = (
    "https://youtube-search-and-download.p.rapidapi.com/channel/search"
)


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

    payload = await rapidapi_post_json(
        host=YOUTUBE_RAPID_HOST,
        url=YOUTUBE_CHANNEL_SEARCH_URL,
        payload=payload_data,
    )
    videos = _extract_videos(payload)
    next_value = str(payload.get("next", "")).strip()
    return {
        "channel_id": clean_channel_id,
        "query": clean_query,
        "videos": videos,
        "next": next_value,
    }
