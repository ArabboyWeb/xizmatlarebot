from typing import Any

from services.rapidapi_client import rapidapi_get

SHAZAM_HOST = "shazam.p.rapidapi.com"
SHAZAM_AUTOCOMPLETE_URL = "https://shazam.p.rapidapi.com/v2/auto-complete"


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


async def shazam_autocomplete(term: str, locale: str = "en-US") -> dict[str, Any]:
    clean_term = (term or "").strip()
    if not clean_term:
        raise ValueError("Shazam qidiruvi uchun matn yuboring.")

    payload = await rapidapi_get(
        host=SHAZAM_HOST,
        url=SHAZAM_AUTOCOMPLETE_URL,
        params={"term": clean_term, "locale": locale},
    )
    hints = _extract_hints(payload)
    tracks = _extract_track_hits(payload)
    return {
        "term": clean_term,
        "hints": hints,
        "tracks": tracks,
    }
