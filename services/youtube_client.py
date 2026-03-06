from __future__ import annotations

import asyncio
import mimetypes
import shutil
from pathlib import Path
from typing import Any

try:
    import yt_dlp
except Exception:  # pragma: no cover
    yt_dlp = None

from services.saver_client import (
    AUDIO_EXTENSIONS,
    DownloadedFile,
    VIDEO_EXTENSIONS,
    _safe_name,
    _target_dir,
    extract_first_url,
    is_youtube_url,
    saver_limit_bytes,
)

VIDEO_QUALITIES = ("best", "1080", "720", "480", "360")
AUDIO_BITRATES = ("128", "192", "256")


def _ydl_base_options() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "cachedir": False,
        "socket_timeout": 40,
        "skip_download": True,
    }


def _duration_text(seconds: Any) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _normalize_entries(info: dict[str, Any], query: str, limit: int) -> dict[str, Any]:
    entries = info.get("entries")
    if not isinstance(entries, list):
        return {"query": query, "videos": []}

    videos: list[dict[str, str]] = []
    for item in entries[:limit]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("webpage_url") or item.get("url") or "").strip()
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        title = str(item.get("title", "")).strip() or "YouTube video"
        uploader = str(item.get("uploader", "")).strip()
        published = str(item.get("upload_date", "")).strip()
        if len(published) == 8 and published.isdigit():
            published = f"{published[0:4]}-{published[4:6]}-{published[6:8]}"
        videos.append(
            {
                "title": title,
                "uploader": uploader,
                "duration": _duration_text(item.get("duration")),
                "published": published,
                "url": url,
            }
        )
    return {"query": query, "videos": videos}


def _pick_video_format(
    info: dict[str, Any],
    *,
    quality: str,
    max_bytes: int,
) -> str:
    formats = info.get("formats")
    if not isinstance(formats, list):
        raise RuntimeError("YouTube formatlari topilmadi.")

    target_height = 0 if quality == "best" else int(quality)
    candidates: list[tuple[tuple[int, int, int, int], str]] = []
    for item in formats:
        if not isinstance(item, dict):
            continue
        format_id = str(item.get("format_id", "")).strip()
        if not format_id:
            continue
        if str(item.get("vcodec", "none")) == "none":
            continue
        if str(item.get("acodec", "none")) == "none":
            continue

        ext = str(item.get("ext", "")).strip().lower()
        if ext and ext not in {ext.strip(".") for ext in VIDEO_EXTENSIONS}:
            pass

        height = int(item.get("height") or 0)
        if target_height and height and height > target_height:
            continue

        size = item.get("filesize") or item.get("filesize_approx")
        size_value = int(size) if isinstance(size, (int, float)) else 0
        if size_value and size_value > max_bytes:
            continue

        tbr = int(item.get("tbr") or 0)
        quality_penalty = abs(target_height - height) if target_height and height else 0
        known_size_penalty = 0 if size_value else 1
        ext_penalty = 0 if ext == "mp4" else 1
        candidates.append(
            (
                (
                    known_size_penalty,
                    quality_penalty,
                    ext_penalty,
                    -tbr,
                ),
                format_id,
            )
        )

    if not candidates:
        raise RuntimeError("Tanlangan sifatda video topilmadi.")

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _pick_audio_format(
    info: dict[str, Any],
    *,
    bitrate: str,
    max_bytes: int,
) -> str:
    formats = info.get("formats")
    if not isinstance(formats, list):
        raise RuntimeError("YouTube audio formatlari topilmadi.")

    target_bitrate = int(bitrate)
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for item in formats:
        if not isinstance(item, dict):
            continue
        format_id = str(item.get("format_id", "")).strip()
        if not format_id:
            continue
        if str(item.get("acodec", "none")) == "none":
            continue
        if str(item.get("vcodec", "none")) != "none":
            continue

        ext = str(item.get("ext", "")).strip().lower()
        size = item.get("filesize") or item.get("filesize_approx")
        size_value = int(size) if isinstance(size, (int, float)) else 0
        if size_value and size_value > max_bytes:
            continue

        abr = int(item.get("abr") or item.get("tbr") or 0)
        bitrate_penalty = abs(target_bitrate - abr) if abr else 9999
        known_size_penalty = 0 if size_value else 1
        ext_penalty = 0 if ext in {"m4a", "mp3"} else 1
        candidates.append(
            (
                (
                    known_size_penalty,
                    bitrate_penalty,
                    ext_penalty,
                ),
                format_id,
            )
        )

    if not candidates:
        raise RuntimeError("Tanlangan sifatda audio topilmadi.")

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _download_youtube_sync(
    url: str,
    *,
    mode: str,
    quality: str,
    audio_bitrate: str,
    max_bytes: int,
) -> DownloadedFile:
    if yt_dlp is None:
        raise RuntimeError("YouTube yuklash moduli o'rnatilmagan.")

    target_dir = _target_dir()
    try:
        base_options = _ydl_base_options()
        base_options["skip_download"] = True
        with yt_dlp.YoutubeDL(base_options) as ydl:
            info = ydl.extract_info(url, download=False)

        if not isinstance(info, dict):
            raise RuntimeError("YouTube ma'lumotlari topilmadi.")
        if info.get("_type") == "playlist":
            raise RuntimeError("Playlist emas, bitta video link yuboring.")

        if mode == "audio":
            format_id = _pick_audio_format(
                info,
                bitrate=audio_bitrate,
                max_bytes=max_bytes,
            )
        else:
            format_id = _pick_video_format(
                info,
                quality=quality,
                max_bytes=max_bytes,
            )

        download_options = _ydl_base_options()
        download_options.update(
            {
                "skip_download": False,
                "format": format_id,
                "outtmpl": str(target_dir / "%(id)s.%(ext)s"),
                "nopart": True,
            }
        )
        with yt_dlp.YoutubeDL(download_options) as ydl:
            ydl.download([url])

        files = [item for item in target_dir.iterdir() if item.is_file()]
        if not files:
            raise RuntimeError("YouTube fayli yuklanmadi.")

        output = max(files, key=lambda item: item.stat().st_mtime)
        size = output.stat().st_size
        if size > max_bytes:
            raise RuntimeError("Fayl limitdan katta.")

        title = str(info.get("title", "")).strip() or output.stem
        safe_name = _safe_name(f"{title}{output.suffix}", fallback=output.name)
        final_output = output
        if output.name != safe_name:
            final_output = output.with_name(safe_name)
            output.rename(final_output)

        content_type = (
            mimetypes.guess_type(final_output.name)[0] or "application/octet-stream"
        )
        return DownloadedFile(
            path=final_output,
            file_name=final_output.name,
            size=size,
            content_type=content_type,
            source="youtube_audio" if mode == "audio" else "youtube_video",
        )
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise


async def search_youtube(query: str, limit: int = 6) -> dict[str, Any]:
    clean_query = (query or "").strip()
    if not clean_query:
        raise ValueError("YouTube qidiruvi uchun matn yuboring.")
    if yt_dlp is None:
        raise RuntimeError("YouTube qidiruv moduli o'rnatilmagan.")

    def run() -> dict[str, Any]:
        options = _ydl_base_options()
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"ytsearch{max(1, min(limit, 8))}:{clean_query}", download=False)
        if not isinstance(info, dict):
            raise RuntimeError("YouTube qidiruv natijasi topilmadi.")
        return _normalize_entries(info, clean_query, max(1, min(limit, 8)))

    return await asyncio.to_thread(run)


async def download_youtube(
    raw_url: str,
    *,
    mode: str = "video",
    quality: str = "best",
    audio_bitrate: str = "192",
    max_bytes: int | None = None,
) -> DownloadedFile:
    url = extract_first_url(raw_url)
    if not is_youtube_url(url):
        raise ValueError("To'g'ri YouTube link yuboring.")
    safe_mode = (mode or "video").strip().lower()
    if safe_mode not in {"video", "audio"}:
        safe_mode = "video"

    safe_quality = (quality or "best").strip().lower()
    if safe_quality not in VIDEO_QUALITIES:
        safe_quality = "best"

    safe_audio_bitrate = str(audio_bitrate or "192").strip()
    if safe_audio_bitrate not in AUDIO_BITRATES:
        safe_audio_bitrate = "192"

    limit = (
        max_bytes if isinstance(max_bytes, int) and max_bytes > 0 else saver_limit_bytes()
    )
    return await asyncio.to_thread(
        _download_youtube_sync,
        url,
        mode=safe_mode,
        quality=safe_quality,
        audio_bitrate=safe_audio_bitrate,
        max_bytes=limit,
    )
