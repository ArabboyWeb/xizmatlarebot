from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:
    import imageio_ffmpeg
except Exception:  # pragma: no cover
    imageio_ffmpeg = None

try:
    import yt_dlp
except Exception:  # pragma: no cover
    yt_dlp = None

from services.load_control import run_in_thread_with_limit
from services.saver_client import (
    DownloadedFile,
    _safe_name,
    _target_dir,
    extract_first_url,
    is_youtube_url,
    saver_limit_bytes,
)

VIDEO_QUALITIES = ("best", "1080", "720", "480", "360")
AUDIO_BITRATES = ("128", "192", "256")
logger = logging.getLogger(__name__)


class _YTDLPLogger:
    def debug(self, message: str) -> None:
        if str(message or "").startswith("[debug]"):
            logger.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        logger.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        logger.warning("yt-dlp error: %s", message)


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
        "retries": 2,
        "fragment_retries": 2,
        "logger": _YTDLPLogger(),
    }


def _ffmpeg_location() -> str:
    if imageio_ffmpeg is None:
        return ""
    try:
        location = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover
        return ""
    return location if location and Path(location).exists() else ""


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


def _video_format_selector(quality: str, *, ffmpeg_location: str) -> str:
    target_height = 1080 if quality == "best" else int(quality)
    progressive = [
        f"best[ext=mp4][height<={target_height}]",
        f"best[height<={target_height}][ext=mp4]",
        "18" if target_height >= 360 else "",
        f"best[height<={target_height}]",
        "best[ext=mp4]",
        "best",
    ]
    if not ffmpeg_location:
        return "/".join(part for part in progressive if part)

    merged = [
        f"bestvideo[ext=mp4][height<={target_height}]+bestaudio[ext=m4a]",
        f"bestvideo[height<={target_height}]+bestaudio",
    ]
    return "/".join(part for part in [*merged, *progressive] if part)


def _audio_format_selector(*, ffmpeg_location: str) -> str:
    if ffmpeg_location:
        return "bestaudio[ext=m4a]/bestaudio"
    return "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"


def _is_streamable_video_suffix(suffix: str) -> bool:
    return suffix.lower() in {".mp4", ".m4v", ".mov"}


def _convert_video_to_mp4(path: Path, *, ffmpeg_location: str) -> Path:
    if not ffmpeg_location or not path.exists():
        return path
    if _is_streamable_video_suffix(path.suffix):
        return path
    target = path.with_suffix(".mp4")
    if target.exists():
        target.unlink(missing_ok=True)
    command = [
        ffmpeg_location,
        "-y",
        "-i",
        str(path),
        "-movflags",
        "+faststart",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        str(target),
    ]
    process = subprocess.run(
        command,
        capture_output=True,
        text=False,
        check=False,
    )
    if process.returncode != 0 or not target.exists() or target.stat().st_size <= 0:
        target.unlink(missing_ok=True)
        return path
    path.unlink(missing_ok=True)
    return target


def _downloaded_files(target_dir: Path) -> list[Path]:
    ignored_suffixes = {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".png", ".webp"}
    return [
        item
        for item in target_dir.iterdir()
        if item.is_file() and item.suffix.lower() not in ignored_suffixes
    ]


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
        ffmpeg_location = _ffmpeg_location()
        download_options = _ydl_base_options()
        download_options.update(
            {
                "skip_download": False,
                "format": (
                    _audio_format_selector(ffmpeg_location=ffmpeg_location)
                    if mode == "audio"
                    else _video_format_selector(
                        quality,
                        ffmpeg_location=ffmpeg_location,
                    )
                ),
                "outtmpl": str(target_dir / "%(id)s.%(ext)s"),
                "nopart": True,
            }
        )
        if ffmpeg_location:
            download_options["ffmpeg_location"] = ffmpeg_location
        if mode == "video" and ffmpeg_location:
            download_options["merge_output_format"] = "mp4"
            download_options["postprocessors"] = [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ]
        if mode == "audio" and ffmpeg_location:
            download_options["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": audio_bitrate,
                }
            ]
            download_options["keepvideo"] = False

        with yt_dlp.YoutubeDL(download_options) as ydl:
            info = ydl.extract_info(url, download=True)

        if not isinstance(info, dict):
            raise RuntimeError("YouTube ma'lumotlari topilmadi.")
        if info.get("_type") == "playlist":
            raise RuntimeError("Playlist emas, bitta video link yuboring.")

        files = _downloaded_files(target_dir)
        if not files:
            raise RuntimeError("YouTube fayli yuklanmadi.")

        output = max(files, key=lambda item: item.stat().st_mtime)
        if mode == "video":
            output = _convert_video_to_mp4(output, ffmpeg_location=ffmpeg_location)
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
        if mode == "video" and not str(content_type).startswith("video/"):
            content_type = "video/mp4"
        if mode == "audio" and not content_type.startswith("audio/"):
            suffix = final_output.suffix.lower()
            if suffix == ".mp3":
                content_type = "audio/mpeg"
            elif suffix in {".m4a", ".mp4"}:
                content_type = "audio/mp4"
            elif suffix in {".webm", ".opus"}:
                content_type = "audio/webm"
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

    return await run_in_thread_with_limit("download", run)


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
    return await run_in_thread_with_limit(
        "download",
        _download_youtube_sync,
        url,
        mode=safe_mode,
        quality=safe_quality,
        audio_bitrate=safe_audio_bitrate,
        max_bytes=limit,
    )
