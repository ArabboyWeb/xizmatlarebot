from pathlib import Path
from typing import Any


def _as_text(value: Any, fallback: str = "") -> str:
    text = str(value).strip() if value is not None else ""
    return text or fallback


def _extract_album(track: dict[str, Any]) -> str:
    sections = track.get("sections")
    if not isinstance(sections, list):
        return ""

    for section in sections:
        if not isinstance(section, dict):
            continue
        metadata = section.get("metadata")
        if not isinstance(metadata, list):
            continue
        for item in metadata:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip().lower()
            if title == "album":
                return _as_text(item.get("text"))
    return ""


def _normalize_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, tuple):
        for item in payload:
            if isinstance(item, dict) and ("track" in item or "matches" in item):
                return item
    raise RuntimeError("Shazam javobi notogri formatda keldi.")


async def recognize_track(file_path: Path) -> dict[str, str]:
    try:
        from shazamio import Shazam
    except Exception as error:  # noqa: BLE001
        raise RuntimeError(
            "ShazamIO o'rnatilmagan. requirements ni yangilang."
        ) from error

    shazam = Shazam()
    raw: Any

    if hasattr(shazam, "recognize_song"):
        raw = await shazam.recognize_song(str(file_path))
    elif hasattr(shazam, "recognize"):
        raw = await shazam.recognize(str(file_path))
    else:
        raise RuntimeError("ShazamIO versiyasi qo'llab-quvvatlanmaydi.")

    payload = _normalize_payload(raw)
    track = payload.get("track")
    if not isinstance(track, dict):
        raise RuntimeError("Musiqa topilmadi yoki aniqlash imkoni bo'lmadi.")

    genres = track.get("genres") if isinstance(track.get("genres"), dict) else {}
    return {
        "title": _as_text(track.get("title"), "Nomalum track"),
        "artist": _as_text(track.get("subtitle"), "Nomalum artist"),
        "album": _extract_album(track),
        "genre": _as_text(genres.get("primary")),
        "url": _as_text(track.get("url")),
        "cover": _as_text(
            (track.get("images") or {}).get("coverart")
            if isinstance(track.get("images"), dict)
            else ""
        ),
    }
