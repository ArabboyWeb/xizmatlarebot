from __future__ import annotations

import contextlib
import mimetypes
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp

from services.load_control import run_with_limit

SAVER_TMP_DIR = Path(__file__).resolve().parent.parent / "downloads_tmp" / "saver"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (XizmatlarBot/1.0; +https://core.telegram.org/bots/api)"
)
HTTP_TIMEOUT_SECONDS = 120
URL_PATTERN = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac", ".opus"}


@dataclass(slots=True)
class DownloadedFile:
    path: Path
    file_name: str
    size: int
    content_type: str
    source: str


def saver_limit_bytes() -> int:
    raw = os.getenv("TELEGRAM_FREE_UPLOAD_LIMIT_MB", "").strip()
    try:
        limit_mb = max(1, int(raw))
    except ValueError:
        limit_mb = 50
    return limit_mb * 1024 * 1024


def extract_first_url(text: str) -> str:
    match = URL_PATTERN.search(text or "")
    if not match:
        raise ValueError("Link topilmadi. To'liq URL yuboring.")
    return match.group(1).strip()


def _safe_name(name: str, fallback: str = "download.bin") -> str:
    clean = re.sub(r"[^\w.\- ]+", "_", unquote(name or "").strip(), flags=re.ASCII)
    clean = clean.strip(" ._")
    return clean or fallback


def is_youtube_url(url: str) -> bool:
    hostname = urlparse(url).hostname or ""
    host = hostname.lower()
    return host in YOUTUBE_HOSTS or any(host.endswith(f".{item}") for item in YOUTUBE_HOSTS)


def detect_send_kind(file_name: str, content_type: str) -> str:
    suffix = Path(file_name).suffix.lower()
    lowered_content_type = (content_type or "").lower()
    if suffix in IMAGE_EXTENSIONS or lowered_content_type.startswith("image/"):
        return "photo"
    if suffix in VIDEO_EXTENSIONS or lowered_content_type.startswith("video/"):
        return "video"
    if suffix in AUDIO_EXTENSIONS or lowered_content_type.startswith("audio/"):
        return "audio"
    return "document"


def _content_disposition_name(header_value: str) -> str:
    if not header_value:
        return ""
    for chunk in header_value.split(";"):
        part = chunk.strip()
        if part.lower().startswith("filename="):
            return part.split("=", maxsplit=1)[1].strip("\"' ")
    return ""


def _looks_like_web_page(
    *,
    path: str,
    content_type: str,
    content_disposition: str,
) -> bool:
    if "attachment" in (content_disposition or "").lower():
        return False
    suffix = Path(path).suffix.lower()
    if content_type.startswith("text/html"):
        return True
    if content_type.startswith("text/") and not suffix:
        return True
    return False


def _target_dir() -> Path:
    target = SAVER_TMP_DIR / uuid.uuid4().hex
    target.mkdir(parents=True, exist_ok=True)
    return target


async def cleanup_download(downloaded: DownloadedFile | None) -> None:
    if downloaded is None:
        return
    with contextlib.suppress(FileNotFoundError):
        downloaded.path.unlink()
    with contextlib.suppress(OSError):
        shutil.rmtree(downloaded.path.parent, ignore_errors=True)


async def download_direct_url(
    url: str,
    max_bytes: int,
    *,
    timeout_seconds: int | None = None,
) -> DownloadedFile:
    async def _run() -> DownloadedFile:
        target_dir = _target_dir()
        timeout = aiohttp.ClientTimeout(
            total=max(5, int(timeout_seconds))
            if isinstance(timeout_seconds, int) and timeout_seconds > 0
            else HTTP_TIMEOUT_SECONDS
        )
        headers = {
            "Accept": "*/*",
            "User-Agent": os.getenv("HTTP_USER_AGENT", "").strip() or DEFAULT_USER_AGENT,
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status >= 400:
                        raise RuntimeError("Linkni yuklab bo'lmadi.")
                    content_length = int(response.headers.get("Content-Length", "0") or 0)
                    if content_length and content_length > max_bytes:
                        raise RuntimeError("Fayl limitdan katta.")

                    parsed = urlparse(str(response.url))
                    content_disposition = response.headers.get("Content-Disposition", "")
                    file_name = _content_disposition_name(content_disposition)
                    if not file_name:
                        file_name = Path(parsed.path).name or "download.bin"

                    content_type = (
                        response.headers.get("Content-Type", "").split(";", maxsplit=1)[0]
                    ).strip() or "application/octet-stream"
                    if _looks_like_web_page(
                        path=parsed.path,
                        content_type=content_type,
                        content_disposition=content_disposition,
                    ):
                        raise ValueError("Bu oddiy sahifa linki. To'g'ridan-to'g'ri fayl link yuboring.")
                    guessed_ext = mimetypes.guess_extension(content_type) or ""
                    suffix = Path(file_name).suffix
                    if not suffix and guessed_ext:
                        file_name = f"{file_name}{guessed_ext}"
                    safe_name = _safe_name(file_name)
                    output = target_dir / safe_name

                    downloaded_bytes = 0
                    with output.open("wb") as file_obj:
                        async for chunk in response.content.iter_chunked(256 * 1024):
                            if not chunk:
                                continue
                            downloaded_bytes += len(chunk)
                            if downloaded_bytes > max_bytes:
                                raise RuntimeError("Fayl limitdan katta.")
                            file_obj.write(chunk)

                    if downloaded_bytes <= 0 or not output.exists():
                        raise RuntimeError("Fayl bo'sh qaytdi.")

                    return DownloadedFile(
                        path=output,
                        file_name=safe_name,
                        size=downloaded_bytes,
                        content_type=content_type,
                        source="direct",
                    )
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise

    return await run_with_limit("download", _run)


async def download_url(url: str, max_bytes: int | None = None) -> DownloadedFile:
    clean_url = extract_first_url(url)
    if is_youtube_url(clean_url):
        raise ValueError("YouTube linklari uchun YouTube bo'limidan foydalaning.")
    limit = (
        max_bytes if isinstance(max_bytes, int) and max_bytes > 0 else saver_limit_bytes()
    )
    return await download_direct_url(clean_url, limit)
