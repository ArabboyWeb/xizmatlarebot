from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

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
    DEFAULT_USER_AGENT,
    _safe_name,
    _target_dir,
    cleanup_download,
    download_direct_url,
    extract_first_url,
    saver_limit_bytes,
)

INSTAGRAM_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
}
TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}
TIKWM_API_URL = "https://www.tikwm.com/api/"
HTTP_TIMEOUT_SECONDS = 40
YTDLP_TIKTOK_TIMEOUT_SECONDS = 15
logger = logging.getLogger(__name__)


class _YTDLPLogger:
    def debug(self, message: str) -> None:
        if str(message or "").startswith("[debug]"):
            logger.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        logger.warning("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        logger.warning("yt-dlp error: %s", message)


def _host_matches(hostname: str, hosts: set[str]) -> bool:
    host = (hostname or "").lower()
    return host in hosts or any(host.endswith(f".{item}") for item in hosts)


def is_instagram_url(url: str) -> bool:
    return _host_matches(urlparse(url).hostname or "", INSTAGRAM_HOSTS)


def is_tiktok_url(url: str) -> bool:
    return _host_matches(urlparse(url).hostname or "", TIKTOK_HOSTS)


def is_social_video_url(url: str) -> bool:
    return is_instagram_url(url) or is_tiktok_url(url)


def social_platform_name(url: str) -> str:
    if is_instagram_url(url):
        return "Instagram"
    if is_tiktok_url(url):
        return "TikTok"
    return "Social video"


def _ydl_base_options(*, socket_timeout: int = 40) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "cachedir": False,
        "socket_timeout": max(5, int(socket_timeout)),
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


def _downloaded_files(target_dir: Path) -> list[Path]:
    ignored_suffixes = {
        ".part",
        ".ytdl",
        ".json",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".vtt",
        ".srt",
        ".ass",
        ".lrc",
        ".ttml",
    }
    return [
        item
        for item in target_dir.iterdir()
        if item.is_file() and item.suffix.lower() not in ignored_suffixes
    ]


def _social_format_selector(*, ffmpeg_location: str) -> str:
    progressive = [
        "best[ext=mp4]",
        "best[ext=webm]",
        "best",
    ]
    if not ffmpeg_location:
        return "/".join(progressive)
    merged = [
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]",
        "bestvideo+bestaudio",
    ]
    return "/".join([*merged, *progressive])


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


def _download_social_video_sync(
    url: str,
    *,
    max_bytes: int,
    socket_timeout: int = 40,
) -> DownloadedFile:
    if yt_dlp is None:
        raise RuntimeError("Social video yuklash moduli o'rnatilmagan.")
    if not is_social_video_url(url):
        raise ValueError("Instagram yoki TikTok link yuboring.")

    platform = social_platform_name(url)
    target_dir = _target_dir()
    try:
        ffmpeg_location = _ffmpeg_location()
        options = _ydl_base_options(socket_timeout=socket_timeout)
        options.update(
            {
                "skip_download": False,
                "format": _social_format_selector(ffmpeg_location=ffmpeg_location),
                "outtmpl": str(target_dir / "%(extractor)s_%(id)s.%(ext)s"),
                "nopart": True,
            }
        )
        if ffmpeg_location:
            options["ffmpeg_location"] = ffmpeg_location
            options["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)

        if not isinstance(info, dict):
            raise RuntimeError(f"{platform} ma'lumotlari topilmadi.")

        files = _downloaded_files(target_dir)
        if not files:
            raise RuntimeError("Video topilmadi yoki private post yuborildi.")

        output = max(
            files,
            key=lambda item: (item.stat().st_size, item.stat().st_mtime),
        )
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
        if not str(content_type).startswith("video/"):
            content_type = "video/mp4"
        source = "instagram_video" if is_instagram_url(url) else "tiktok_video"
        return DownloadedFile(
            path=final_output,
            file_name=final_output.name,
            size=size,
            content_type=content_type,
            source=source,
        )
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise


def _retag_download(
    downloaded: DownloadedFile,
    *,
    title: str,
    source: str,
) -> DownloadedFile:
    guessed_suffix = mimetypes.guess_extension(str(downloaded.content_type or "").strip())
    suffix = downloaded.path.suffix or Path(downloaded.file_name).suffix or guessed_suffix or ".mp4"
    if suffix.lower() in {".bin", ".tmp"}:
        suffix = guessed_suffix or ".mp4"
    desired_name = _safe_name(f"{title}{suffix}", fallback=downloaded.file_name)
    final_path = downloaded.path
    if downloaded.file_name != desired_name:
        final_path = downloaded.path.with_name(desired_name)
        downloaded.path.rename(final_path)
    content_type = (
        downloaded.content_type
        or mimetypes.guess_type(final_path.name)[0]
        or "video/mp4"
    )
    if not str(content_type).startswith("video/"):
        content_type = "video/mp4"
    return DownloadedFile(
        path=final_path,
        file_name=final_path.name,
        size=downloaded.size,
        content_type=content_type,
        source=source,
    )


async def _fetch_tikwm_payload(url: str) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    headers = {
        "Accept": "application/json",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    for attempt in range(2):
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.post(
                TIKWM_API_URL,
                data={"url": url, "hd": "1"},
            ) as response:
                if response.status >= 400:
                    raise RuntimeError("TikTok fallback xizmati javob bermadi.")
                try:
                    payload = await response.json(content_type=None)
                except Exception as error:  # noqa: BLE001
                    raise RuntimeError("TikTok fallback javobi noto'g'ri.") from error
        if not isinstance(payload, dict):
            raise RuntimeError("TikTok fallback javobi noto'g'ri.")
        raw_code = payload.get("code", -1)
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = -1
        if code == 0:
            data = payload.get("data")
            if not isinstance(data, dict):
                raise RuntimeError("TikTok video ma'lumotlari topilmadi.")
            return data
        message = str(payload.get("msg", "") or "TikTok fallback xatosi.").strip()
        if "limit" in message.lower() and attempt == 0:
            await asyncio.sleep(1.2)
            continue
        raise RuntimeError(message)
    raise RuntimeError("TikTok fallback xizmati vaqtincha band.")


async def _download_tiktok_via_tikwm(url: str, *, max_bytes: int) -> DownloadedFile:
    data = await _fetch_tikwm_payload(url)
    title = (
        str(data.get("title", "")).strip()
        or str(data.get("id", "")).strip()
        or "TikTok video"
    )
    candidates: list[tuple[str, int]] = []
    for key, size_key in (
        ("hdplay", "hd_size"),
        ("play", "size"),
        ("wmplay", "wm_size"),
    ):
        media_url = str(data.get(key, "")).strip()
        if not media_url:
            continue
        try:
            media_size = int(data.get(size_key, 0) or 0)
        except (TypeError, ValueError):
            media_size = 0
        if media_size > 0 and media_size > max_bytes:
            continue
        if media_url not in {item[0] for item in candidates}:
            candidates.append((media_url, media_size))
    if not candidates:
        raise RuntimeError("TikTok video linki topilmadi.")

    last_error: Exception | None = None
    for media_url, _media_size in candidates:
        downloaded: DownloadedFile | None = None
        try:
            downloaded = await download_direct_url(
                media_url,
                max_bytes,
                timeout_seconds=15,
            )
            return _retag_download(
                downloaded,
                title=title,
                source="tiktok_video",
            )
        except Exception as error:  # noqa: BLE001
            last_error = error
            await cleanup_download(downloaded)
    if last_error is not None:
        raise last_error
    raise RuntimeError("TikTok video linki topilmadi.")


async def download_social_video(
    raw_url: str,
    *,
    max_bytes: int | None = None,
) -> DownloadedFile:
    url = extract_first_url(raw_url)
    if not is_social_video_url(url):
        raise ValueError("Instagram yoki TikTok link yuboring.")
    limit = (
        max_bytes if isinstance(max_bytes, int) and max_bytes > 0 else saver_limit_bytes()
    )
    if is_tiktok_url(url):
        try:
            return await _download_tiktok_via_tikwm(url, max_bytes=limit)
        except Exception as tikwm_error:
            try:
                return await run_in_thread_with_limit(
                    "download",
                    _download_social_video_sync,
                    url,
                    max_bytes=limit,
                    socket_timeout=YTDLP_TIKTOK_TIMEOUT_SECONDS,
                )
            except Exception as ytdlp_error:
                lowered = " ".join(
                    [
                        str(tikwm_error or "").lower(),
                        str(ytdlp_error or "").lower(),
                    ]
                )
                if "limit" in lowered or "katta" in lowered:
                    raise RuntimeError("Fayl limitdan katta.")
                if "private" in lowered or "login" in lowered or "sign in" in lowered:
                    raise RuntimeError("Private yoki cheklangan video yuborildi.")
                if "timeout" in lowered or "handshake" in lowered or "ssl" in lowered:
                    raise RuntimeError(
                        "TikTok CDN javob bermadi. Birozdan keyin qayta urinib ko'ring."
                    ) from ytdlp_error
                raise RuntimeError(
                    "TikTok videoni yuklab bo'lmadi. Public video link yuboring."
                ) from ytdlp_error
    return await run_in_thread_with_limit(
        "download",
        _download_social_video_sync,
        url,
        max_bytes=limit,
    )
