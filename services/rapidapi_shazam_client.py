from typing import Any

import aiohttp

from services.rapidapi_client import HTTP_TIMEOUT_SECONDS, rapidapi_get

SHAZAM_HOST = "shazam.p.rapidapi.com"
SHAZAM_AUTOCOMPLETE_URL = "https://shazam.p.rapidapi.com/v2/auto-complete"
DEEZER_SEARCH_URL = "https://api.deezer.com/search"


def _extract_hints(payload: dict[str, Any]) -> list[str]:
    hints = payload.get("hints")
    if not isinstance(hints, list):
        return []
    result: list[str] = []
    for item in hints:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, dict):
            text = str(item.get("term", "")).strip()
            if text:
                result.append(text)
    return result


def _extract_track_hits(payload: dict[str, Any]) -> list[dict[str, str]]:
    tracks = payload.get("tracks")
    if not isinstance(tracks, dict):
        return []
    hits = tracks.get("hits")
    if not isinstance(hits, list):
        return []

    rows: list[dict[str, str]] = []
    for item in hits:
        if not isinstance(item, dict):
            continue
        track = item.get("track")
        if not isinstance(track, dict):
            continue
        title = str(track.get("title", "")).strip()
        subtitle = str(track.get("subtitle", "")).strip()
        if title:
            rows.append(
                {
                    "title": title,
                    "subtitle": subtitle,
                }
            )
    return rows


def _extract_deezer_tracks(payload: dict[str, Any]) -> list[dict[str, str]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    tracks: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        artist = item.get("artist")
        subtitle = ""
        if isinstance(artist, dict):
            subtitle = str(artist.get("name", "")).strip()
        if not subtitle:
            subtitle = str(item.get("artist", "")).strip()
        tracks.append({"title": title, "subtitle": subtitle})
    return tracks


def _fallback_hints(tracks: list[dict[str, str]]) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()
    for row in tracks:
        for candidate in (row.get("title", ""), row.get("subtitle", "")):
            value = str(candidate or "").strip()
            key = value.lower()
            if not value or key in seen:
                continue
            hints.append(value)
            seen.add(key)
            if len(hints) >= 8:
                return hints
    return hints


async def _deezer_search(term: str) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (XizmatlarBot/1.0)",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(DEEZER_SEARCH_URL, params={"q": term}) as response:
            payload = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(f"Deezer fallback xatosi: HTTP {response.status}")

    if not isinstance(payload, dict):
        raise RuntimeError("Deezer fallback noto'g'ri formatda javob qaytardi.")
    return payload


async def shazam_autocomplete(term: str, locale: str = "en-US") -> dict[str, Any]:
    clean_term = (term or "").strip()
    if not clean_term:
        raise ValueError("Shazam qidiruvi uchun matn yuboring.")

    rapidapi_error: Exception | None = None
    try:
        payload = await rapidapi_get(
            host=SHAZAM_HOST,
            url=SHAZAM_AUTOCOMPLETE_URL,
            params={"term": clean_term, "locale": locale},
        )
        hints = _extract_hints(payload)
        tracks = _extract_track_hits(payload)
        if hints or tracks:
            return {
                "term": clean_term,
                "hints": hints,
                "tracks": tracks,
            }
    except Exception as error:  # noqa: BLE001
        rapidapi_error = error

    try:
        fallback_payload = await _deezer_search(clean_term)
        tracks = _extract_deezer_tracks(fallback_payload)
        return {
            "term": clean_term,
            "hints": _fallback_hints(tracks),
            "tracks": tracks,
        }
    except Exception as fallback_error:  # noqa: BLE001
        if rapidapi_error is not None:
            raise RuntimeError(
                f"Shazam qidiruvi ishlamadi. RapidAPI: {rapidapi_error}. "
                f"Fallback: {fallback_error}."
            ) from fallback_error
        raise

    return {
        "term": clean_term,
        "hints": [],
        "tracks": [],
    }
