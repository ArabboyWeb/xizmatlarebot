import asyncio
import contextlib
import ipaddress
import logging
import mimetypes
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import AsyncGenerator, Callable, Optional
from urllib.parse import unquote, urlsplit

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.types.input_file import FSInputFile
from dotenv import load_dotenv
import psutil

try:
    import yt_dlp
except Exception:  # pragma: no cover
    yt_dlp = None

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
MAX_TELEGRAM_FILE_BYTES = 4 * 1024 * 1024 * 1024
URL_PATTERN = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
FILE_NAME_SAFE = re.compile(r"[^A-Za-z0-9._ -]")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (DownloaderBot/3.0; +https://core.telegram.org/bots/api)"
)
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
BLOCKED_HOSTS = {"localhost"}
BOT_TOKEN_PATTERN = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
MIN_PARALLEL_DOWNLOAD_BYTES = 32 * 1024 * 1024
FILE_IO_BUFFER_BYTES = 8 * 1024 * 1024
MODULE_DIR = Path(__file__).resolve().parent
BASE_DIR = (
    MODULE_DIR.parent.parent if MODULE_DIR.parent.name == "functions" else MODULE_DIR
)

# MIME → Telegram send method mapping
VIDEO_MIME_PREFIXES = {"video/"}
AUDIO_MIME_PREFIXES = {"audio/"}
IMAGE_MIME_PREFIXES = {"image/"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpeg", ".mpg"}
AUDIO_EXTENSIONS = {".mp3", ".ogg", ".flac", ".wav", ".aac", ".m4a", ".opus", ".wma"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# ──────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────


class AppError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class InvalidLinkError(AppError):
    pass


class NonDirectLinkError(AppError):
    pass


class FileTooLargeError(AppError):
    pass


class DownloadNetworkError(AppError):
    pass


class AccessDeniedError(AppError):
    pass


class RangeUnsupportedError(AppError):
    pass


class SendFailedError(AppError):
    pass


class TransientDownloadError(Exception):
    pass


# ──────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────


@dataclass(slots=True)
class DownloadResult:
    temp_path: Path
    output_filename: str
    total_size: int
    content_type: str


@dataclass(slots=True)
class SendReport:
    elapsed_seconds: float
    avg_speed_bytes_per_second: float
    sent_as: str


@dataclass(slots=True)
class Config:
    bot_token: str
    bot_api_base: Optional[str]
    temp_dir: Path
    max_file_bytes: int
    concurrent_downloads: int
    per_user_download_limit: int
    progress_interval_seconds: float
    send_progress_interval_seconds: float
    connect_timeout_seconds: int
    read_timeout_seconds: int
    max_retries: int
    retry_backoff_seconds: float
    polling_restart_delay_seconds: int
    chunk_size_kb: int
    upload_chunk_kb: int
    download_workers: int
    parallel_download_min_bytes: int
    http_connector_limit: int
    http_connector_limit_per_host: int
    process_monitor_interval_seconds: int
    prefer_native_media: bool
    telegram_url_send: bool
    native_media_max_bytes: int
    max_url_length: int
    user_agent: str
    allowed_user_ids: set[int]
    cleanup_after_hours: int
    log_file: Path
    lock_file: Path
    send_max_retries: int
    send_retry_backoff_seconds: float
    upload_timeout_seconds: int
    youtube_enabled: bool
    youtube_timeout_seconds: int
    youtube_max_duration_seconds: int
    youtube_prefer_mp4: bool


# ──────────────────────────────────────────────
# Config helpers
# ──────────────────────────────────────────────


def read_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def read_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def read_csv_int_set(name: str) -> set[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return set()
    ids: set[int] = set()
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        with contextlib.suppress(ValueError):
            ids.add(int(value))
    return ids


def resolve_app_path(raw_value: str, default_value: str) -> Path:
    candidate = (
        Path(raw_value.strip())
        if raw_value and raw_value.strip()
        else Path(default_value)
    )
    if candidate.is_absolute():
        return candidate
    return (BASE_DIR / candidate).resolve()


def load_config() -> Config:
    load_dotenv(override=True)
    token = os.getenv("BOT_TOKEN", "").strip().strip("\"'")
    if not token:
        raise ValueError("BOT_TOKEN topilmadi. .env ichiga BOT_TOKEN kiriting.")
    if not BOT_TOKEN_PATTERN.fullmatch(token):
        raise ValueError(
            "BOT_TOKEN format noto'g'ri. BotFather'dan yangi token olib .env ga to'g'ri kiriting."
        )

    requested_mb = read_int("MAX_FILE_SIZE_MB", 4096)
    requested_bytes = max(1, requested_mb) * 1024 * 1024
    max_file_bytes = min(requested_bytes, MAX_TELEGRAM_FILE_BYTES)
    parallel_min_mb = max(1, read_int("PARALLEL_DOWNLOAD_MIN_MB", 64))
    native_media_max_mb = max(10, read_int("NATIVE_MEDIA_MAX_MB", 1024))

    return Config(
        bot_token=token,
        bot_api_base=os.getenv("BOT_API_BASE", "").strip() or None,
        temp_dir=resolve_app_path(
            os.getenv("TEMP_DIR", "downloads_tmp"), "downloads_tmp"
        ),
        max_file_bytes=max_file_bytes,
        concurrent_downloads=max(1, read_int("CONCURRENT_DOWNLOADS", 4)),
        per_user_download_limit=max(1, read_int("PER_USER_DOWNLOAD_LIMIT", 1)),
        progress_interval_seconds=max(
            2.0, read_float("PROGRESS_INTERVAL_SECONDS", 4.0)
        ),
        send_progress_interval_seconds=max(
            1.0, read_float("SEND_PROGRESS_INTERVAL_SECONDS", 3.0)
        ),
        connect_timeout_seconds=max(10, read_int("CONNECT_TIMEOUT_SECONDS", 30)),
        read_timeout_seconds=max(30, read_int("READ_TIMEOUT_SECONDS", 120)),
        max_retries=max(1, read_int("MAX_RETRIES", 3)),
        retry_backoff_seconds=max(1.0, read_float("RETRY_BACKOFF_SECONDS", 2.0)),
        polling_restart_delay_seconds=max(
            3, read_int("POLLING_RESTART_DELAY_SECONDS", 8)
        ),
        chunk_size_kb=min(8192, max(128, read_int("DOWNLOAD_CHUNK_KB", 4096))),
        upload_chunk_kb=min(4096, max(64, read_int("UPLOAD_CHUNK_KB", 2048))),
        download_workers=max(1, min(8, read_int("DOWNLOAD_WORKERS", 4))),
        parallel_download_min_bytes=parallel_min_mb * 1024 * 1024,
        http_connector_limit=max(20, read_int("HTTP_CONNECTOR_LIMIT", 300)),
        http_connector_limit_per_host=max(
            5, read_int("HTTP_CONNECTOR_LIMIT_PER_HOST", 100)
        ),
        process_monitor_interval_seconds=max(
            0,
            read_int(
                "PROCESS_MONITOR_INTERVAL_SECONDS",
                30 if read_bool("ENABLE_PROCESS_MONITOR", True) else 0,
            ),
        ),
        prefer_native_media=read_bool("PREFER_NATIVE_MEDIA", True),
        telegram_url_send=read_bool("TELEGRAM_URL_SEND", True),
        native_media_max_bytes=min(max_file_bytes, native_media_max_mb * 1024 * 1024),
        max_url_length=max(256, min(4096, read_int("MAX_URL_LENGTH", 2048))),
        user_agent=os.getenv("HTTP_USER_AGENT", DEFAULT_USER_AGENT).strip()
        or DEFAULT_USER_AGENT,
        allowed_user_ids=read_csv_int_set("ALLOWED_USER_IDS"),
        cleanup_after_hours=max(1, read_int("CLEANUP_AFTER_HOURS", 24)),
        log_file=resolve_app_path(
            os.getenv("LOG_FILE", "logs/bot.log"), "logs/bot.log"
        ),
        lock_file=resolve_app_path(os.getenv("LOCK_FILE", "bot.lock"), "bot.lock"),
        # ── NEW send config ──────────────────────────────────────────────
        send_max_retries=max(1, read_int("SEND_MAX_RETRIES", 4)),
        send_retry_backoff_seconds=max(
            1.0, read_float("SEND_RETRY_BACKOFF_SECONDS", 3.0)
        ),
        upload_timeout_seconds=max(
            120, read_int("UPLOAD_TIMEOUT_SECONDS", 1800)
        ),  # 30 min default
        youtube_enabled=read_bool("YOUTUBE_ENABLED", True),
        youtube_timeout_seconds=max(120, read_int("YOUTUBE_TIMEOUT_SECONDS", 3600)),
        youtube_max_duration_seconds=max(
            0, read_int("YOUTUBE_MAX_DURATION_SECONDS", 0)
        ),
        youtube_prefer_mp4=read_bool("YOUTUBE_PREFER_MP4", True),
    )


# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────


def setup_logging(config: Config) -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    level = getattr(logging, level_name, logging.INFO)
    config.log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        filename=config.log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)


# ──────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────


def format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def sanitize_file_name(name: str) -> str:
    cleaned = name.replace("\\", "_").replace("/", "_").strip()
    cleaned = FILE_NAME_SAFE.sub("_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = f"file_{uuid.uuid4().hex[:8]}"
    return cleaned[:180]


def filename_from_headers(url: str, headers: aiohttp.typedefs.LooseHeaders) -> str:
    content_disposition = str(headers.get("Content-Disposition", ""))
    file_name = ""

    utf8_match = re.search(
        r"filename\*\s*=\s*UTF-8''([^;]+)", content_disposition, flags=re.IGNORECASE
    )
    if utf8_match:
        file_name = unquote(utf8_match.group(1))
    else:
        basic_match = re.search(
            r'filename\s*=\s*"([^"]+)"', content_disposition, flags=re.IGNORECASE
        )
        if basic_match:
            file_name = basic_match.group(1)
        else:
            basic_match = re.search(
                r"filename\s*=\s*([^;]+)", content_disposition, flags=re.IGNORECASE
            )
            if basic_match:
                file_name = basic_match.group(1).strip()

    if not file_name:
        path_name = Path(unquote(urlsplit(url).path)).name
        file_name = path_name or f"file_{uuid.uuid4().hex[:8]}"

    return sanitize_file_name(file_name)


def message_text_and_entities(message: Message) -> tuple[str, list]:
    text = (message.text or message.caption or "").strip()
    entities = message.entities or message.caption_entities or []
    return text, list(entities)


def extract_url(message: Message) -> Optional[str]:
    text, entities = message_text_and_entities(message)
    if entities and text:
        for entity in entities:
            if entity.type == "text_link" and entity.url:
                return str(entity.url).strip()
            if entity.type == "url":
                value = text[entity.offset : entity.offset + entity.length]
                return value.strip()

    if not text:
        return None

    match = URL_PATTERN.search(text)
    if not match:
        return None
    return match.group(1).rstrip(").,]")


def host_is_blocked(hostname: str) -> bool:
    lower_host = hostname.lower().strip(".")
    if lower_host in BLOCKED_HOSTS or lower_host.endswith(".local"):
        return True
    with contextlib.suppress(ValueError):
        ip = ipaddress.ip_address(lower_host)
        return any(
            [
                ip.is_private,
                ip.is_loopback,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
                ip.is_unspecified,
            ]
        )
    return False


def normalize_url(url: str, max_url_length: int) -> str:
    candidate = url.strip()
    if len(candidate) > max_url_length:
        raise InvalidLinkError(
            f"URL juda uzun. Maksimum uzunlik: {max_url_length} belgi."
        )

    if candidate.startswith("www."):
        candidate = f"https://{candidate}"

    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise InvalidLinkError("Faqat http yoki https link qabul qilinadi.")
    if not parsed.netloc:
        raise InvalidLinkError("URL noto'g'ri: host topilmadi.")
    if not parsed.hostname:
        raise InvalidLinkError("URL noto'g'ri: hostname topilmadi.")
    if host_is_blocked(parsed.hostname):
        raise InvalidLinkError(
            "Local/private tarmoqqa linklar xavfsizlik sababli bloklangan."
        )
    return candidate


def is_youtube_url(url: str) -> bool:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().strip(".")
    return hostname in YOUTUBE_HOSTS


def looks_like_direct_link(headers: aiohttp.typedefs.LooseHeaders) -> bool:
    content_type = str(headers.get("Content-Type", "")).lower()
    content_disposition = str(headers.get("Content-Disposition", "")).lower()
    if content_type.startswith("text/html") and "attachment" not in content_disposition:
        return False
    return True


def parse_content_length(headers: aiohttp.typedefs.LooseHeaders) -> Optional[int]:
    raw_length = headers.get("Content-Length")
    if not raw_length:
        return None
    try:
        parsed = int(str(raw_length))
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def retry_delay(base_seconds: float, attempt: int) -> float:
    return min(60.0, base_seconds * (2 ** max(0, attempt - 1)))


def supports_range_download(headers: aiohttp.typedefs.LooseHeaders) -> bool:
    return "bytes" in str(headers.get("Accept-Ranges", "")).lower()


def build_ranges(total_bytes: int, parts: int) -> list[tuple[int, int]]:
    if total_bytes <= 0:
        return []
    part_size = total_bytes // parts
    ranges: list[tuple[int, int]] = []
    start = 0
    for index in range(parts):
        if index == parts - 1:
            end = total_bytes - 1
        else:
            end = start + part_size - 1
        if end < start:
            end = start
        ranges.append((start, end))
        start = end + 1
    return ranges


def detect_send_kind(content_type: str, filename: str) -> str:
    """Detect best Telegram send method: 'video', 'audio', 'photo', 'document'."""
    ct = content_type.lower().strip()
    ext = Path(filename).suffix.lower()

    if ct.startswith("video/") or ext in VIDEO_EXTENSIONS:
        return "video"
    if ct.startswith("audio/") or ext in AUDIO_EXTENSIONS:
        return "audio"
    if (ct.startswith("image/") and "gif" not in ct) or ext in IMAGE_EXTENSIONS:
        return "photo"
    return "document"


def telegram_error_message(error: TelegramBadRequest, max_bytes: int) -> str:
    message = (error.message or "").lower()
    if "file is too big" in message or "request entity too large" in message:
        return (
            f"Telegram faylni qabul qilmadi. Hajm limitga urildi. "
            f"Hozirgi bot limiti: {format_bytes(max_bytes)}."
        )
    if "wrong file identifier" in message or "http url specified" in message:
        return "Telegram bu faylni qabul qila olmadi. Fayl formati yaroqsiz bo'lishi mumkin."
    if "file must be non-empty" in message:
        return "Yuborilgan fayl bo'sh. Qayta yuklang."
    return f"Telegram yuborishda xatolik: {error.message}"


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except Exception:
        if os.name == "nt":
            try:
                output = subprocess.check_output(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    text=True,
                    stderr=subprocess.STDOUT,
                )
            except Exception:
                return True
            return str(pid) in output
        return True
    return True


# ──────────────────────────────────────────────
# Instance lock
# ──────────────────────────────────────────────


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._try_acquire_once()

    def _try_acquire_once(self) -> None:
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self._handle_existing_lock()
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)

        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            lock_file.write(f"{os.getpid()}\n")
            lock_file.write(f"{time.time():.0f}\n")
        self.acquired = True

    def _handle_existing_lock(self) -> None:
        existing_pid = -1
        with contextlib.suppress(OSError, ValueError):
            with self.path.open("r", encoding="utf-8") as lock_file:
                first_line = lock_file.readline().strip()
            existing_pid = int(first_line)

        if is_process_alive(existing_pid):
            raise ValueError(
                f"Boshqa bot instance ishlayapti (pid={existing_pid}). Avval uni to'xtating."
            )

        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def release(self) -> None:
        if not self.acquired:
            return
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        self.acquired = False


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


async def safe_edit(message: Message, text: str) -> None:
    """Edit message text, suppressing all non-critical errors."""
    with contextlib.suppress(
        TelegramBadRequest,
        TelegramNetworkError,
        TelegramRetryAfter,
        aiohttp.ClientError,
        asyncio.TimeoutError,
    ):
        await message.edit_text(text)


class ProgressFSInputFile(FSInputFile):
    """FSInputFile wrapper that reports uploaded bytes while streaming."""

    def __init__(
        self,
        path: Path | str,
        filename: Optional[str] = None,
        chunk_size: int = 65536,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__(path=path, filename=filename, chunk_size=chunk_size)
        self._progress_callback = progress_callback

    async def read(self, bot: Bot) -> AsyncGenerator[bytes, None]:
        sent_bytes = 0
        async for chunk in super().read(bot):
            sent_bytes += len(chunk)
            if self._progress_callback is not None:
                self._progress_callback(sent_bytes)
            yield chunk


# ──────────────────────────────────────────────
# Main bot class
# ──────────────────────────────────────────────


class DirectLinkDownloaderBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.download_semaphore = asyncio.Semaphore(config.concurrent_downloads)
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.active_per_user: dict[int, int] = {}
        self.user_lock = asyncio.Lock()
        self.user_tasks: dict[int, asyncio.Task] = {}
        self.user_task_lock = asyncio.Lock()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.started_at = time.time()
        self.completed_downloads = 0
        self.failed_downloads = 0
        self.total_downloaded_bytes = 0
        self.monitor_task: Optional[asyncio.Task] = None
        self.process = psutil.Process(os.getpid())
        self.process.cpu_percent(None)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def startup(self) -> None:
        self.config.temp_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_stale_temp_files()
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=self.config.connect_timeout_seconds,
            sock_connect=self.config.connect_timeout_seconds,
            sock_read=self.config.read_timeout_seconds,
        )
        connector = aiohttp.TCPConnector(
            limit=self.config.http_connector_limit,
            limit_per_host=self.config.http_connector_limit_per_host,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        self.http_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            auto_decompress=False,
            read_bufsize=max(256 * 1024, self.config.chunk_size_kb * 1024),
        )
        if self.config.process_monitor_interval_seconds > 0:
            self.monitor_task = asyncio.create_task(
                self._monitor_loop(), name="process_monitor"
            )

    async def shutdown(self) -> None:
        if self.monitor_task is not None:
            self.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitor_task
        if self.http_session is not None:
            await self.http_session.close()

    async def _monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.process_monitor_interval_seconds)
            self.logger.info(
                "Process monitor | %s",
                self.build_process_snapshot().replace("\n", " | "),
            )

    # ── Temp file cleanup ─────────────────────────────────────────────────

    def cleanup_stale_temp_files(self) -> None:
        cutoff = time.time() - self.config.cleanup_after_hours * 3600
        for file_path in self.config.temp_dir.glob("*.part"):
            with contextlib.suppress(OSError):
                if file_path.stat().st_mtime < cutoff:
                    file_path.unlink()

    async def safe_remove_temp_file(self, file_path: Path) -> bool:
        for attempt in range(5):
            try:
                file_path.unlink()
                return True
            except FileNotFoundError:
                return True
            except PermissionError:
                if attempt == 4:
                    self.logger.warning(
                        "Temp fayl bandligi sabab o'chmadi: %s", file_path
                    )
                    return False
                await asyncio.sleep(0.4 * (attempt + 1))
            except OSError as error:
                self.logger.warning(
                    "Temp fayl o'chirishda xato: %s | %s", file_path, error
                )
                return False
        return False

    # ── Access / slots ────────────────────────────────────────────────────

    def user_has_access(self, user_id: Optional[int]) -> bool:
        if not self.config.allowed_user_ids:
            return True
        if user_id is None:
            return False
        return user_id in self.config.allowed_user_ids

    async def acquire_user_slot(self, user_id: Optional[int]) -> bool:
        if user_id is None:
            return True
        async with self.user_lock:
            active = self.active_per_user.get(user_id, 0)
            if active >= self.config.per_user_download_limit:
                return False
            self.active_per_user[user_id] = active + 1
            return True

    async def release_user_slot(self, user_id: Optional[int]) -> None:
        if user_id is None:
            return
        async with self.user_lock:
            active = self.active_per_user.get(user_id, 0)
            if active <= 1:
                self.active_per_user.pop(user_id, None)
            else:
                self.active_per_user[user_id] = active - 1

    async def set_user_task(self, user_id: Optional[int], task: asyncio.Task) -> None:
        if user_id is None:
            return
        async with self.user_task_lock:
            self.user_tasks[user_id] = task

    async def clear_user_task(self, user_id: Optional[int], task: asyncio.Task) -> None:
        if user_id is None:
            return
        async with self.user_task_lock:
            existing = self.user_tasks.get(user_id)
            if existing is task:
                self.user_tasks.pop(user_id, None)

    async def cancel_user_task(self, user_id: Optional[int]) -> bool:
        if user_id is None:
            return False
        async with self.user_task_lock:
            task = self.user_tasks.get(user_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    # ── Process snapshot ──────────────────────────────────────────────────

    def build_process_snapshot(self) -> str:
        uptime_seconds = max(1, int(time.time() - self.started_at))
        active_users = len(self.active_per_user)
        active_global = self.config.concurrent_downloads - getattr(
            self.download_semaphore, "_value", 0
        )
        if active_global < 0:
            active_global = 0
        try:
            cpu_percent = self.process.cpu_percent(None)
            rss = self.process.memory_info().rss
            threads = self.process.num_threads()
        except (psutil.Error, OSError):
            cpu_percent = 0.0
            rss = 0
            threads = 0
        dual_mode = "on" if self.config.download_workers >= 2 else "off"
        return (
            f"PID: {os.getpid()}\n"
            f"Uptime: {format_duration(uptime_seconds)}\n"
            f"CPU: {cpu_percent:.1f}%\n"
            f"RAM: {format_bytes(rss)}\n"
            f"Threads: {threads}\n"
            f"Active downloads: {active_global}/{self.config.concurrent_downloads}\n"
            f"Active users: {active_users}\n"
            f"Completed: {self.completed_downloads}\n"
            f"Failed: {self.failed_downloads}\n"
            f"Downloaded total: {format_bytes(self.total_downloaded_bytes)}\n"
            f"Dual workers: {dual_mode} ({self.config.download_workers})"
        )

    # ── Request headers ───────────────────────────────────────────────────

    def _request_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.config.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }

    # ──────────────────────────────────────────────────────────────────────
    # SENDING (FIXED)
    # ──────────────────────────────────────────────────────────────────────

    async def _send_download_result(
        self,
        message: Message,
        status_message: Message,
        download_result: DownloadResult,
    ) -> SendReport:
        """
        Send the downloaded file to Telegram with:
        - Smart media type detection (video / audio / photo / document)
        - Retry with exponential backoff
        - Upload timeout protection
        - Progress updates
        - Automatic fallback to document on media send failure
        """
        caption = (
            f"📁 <b>{download_result.output_filename}</b>\n"
            f"📦 Hajmi: {format_bytes(download_result.total_size)}"
        )

        kind = "document"
        if self.config.prefer_native_media:
            kind = detect_send_kind(
                download_result.content_type, download_result.output_filename
            )

        last_error: Optional[Exception] = None

        for attempt in range(1, self.config.send_max_retries + 1):
            try:
                return await asyncio.wait_for(
                    self._attempt_send(
                        message=message,
                        status_message=status_message,
                        download_result=download_result,
                        caption=caption,
                        kind=kind,
                        attempt=attempt,
                    ),
                    timeout=self.config.upload_timeout_seconds,
                )
            except asyncio.TimeoutError as e:
                last_error = e
                self.logger.warning(
                    "Send attempt %d/%d timed out after %ds",
                    attempt,
                    self.config.send_max_retries,
                    self.config.upload_timeout_seconds,
                )
                await safe_edit(
                    status_message,
                    f"⏳ Yuborish vaqti tugadi ({attempt}/{self.config.send_max_retries}). Qayta urinilmoqda...",
                )
            except TelegramRetryAfter as e:
                last_error = e
                wait = max(int(e.retry_after), 5)
                self.logger.warning("Telegram rate limit: retry after %ds", wait)
                await safe_edit(
                    status_message,
                    f"⏳ Telegram limit: {wait}s kutilmoqda ({attempt}/{self.config.send_max_retries})...",
                )
                await asyncio.sleep(wait)
                continue  # Don't add extra backoff delay after TelegramRetryAfter
            except TelegramBadRequest as e:
                msg = (e.message or "").lower()
                # If media type was rejected, fall back to document immediately
                if kind != "document" and (
                    "wrong type" in msg
                    or "failed to get" in msg
                    or "unsupported" in msg
                    or "bad request" in msg
                ):
                    self.logger.warning(
                        "Send as '%s' failed (%s), falling back to document.",
                        kind,
                        e.message,
                    )
                    kind = "document"
                    continue
                raise  # Non-recoverable bad request
            except (TelegramNetworkError, aiohttp.ClientError, OSError) as e:
                last_error = e
                self.logger.warning(
                    "Send attempt %d/%d failed: %s",
                    attempt,
                    self.config.send_max_retries,
                    e,
                )
                await safe_edit(
                    status_message,
                    f"⚠️ Yuborish xatosi ({attempt}/{self.config.send_max_retries}). Qayta urinilmoqda...",
                )
            except asyncio.CancelledError:
                raise

            if attempt < self.config.send_max_retries:
                delay = retry_delay(self.config.send_retry_backoff_seconds, attempt)
                await asyncio.sleep(delay)

        raise SendFailedError(
            f"Fayl {self.config.send_max_retries} marta yuborishga urinildi, lekin muvaffaqiyatsiz. "
            f"Keyinroq qayta urinib ko'ring. Xato: {last_error}"
        )

    async def _attempt_send(
        self,
        message: Message,
        status_message: Message,
        download_result: DownloadResult,
        caption: str,
        kind: str,
        attempt: int,
    ) -> SendReport:
        """Single send attempt with progress updates."""
        file_path = download_result.temp_path
        file_name = download_result.output_filename

        # Verify file exists and is readable before attempting send
        if not file_path.exists():
            raise SendFailedError(
                "Temp fayl topilmadi. Iltimos, qaytadan link yuboring."
            )
        if file_path.stat().st_size == 0:
            raise SendFailedError("Temp fayl bo'sh. Qaytadan urinib ko'ring.")

        upload_chunk_bytes = self.config.upload_chunk_kb * 1024
        send_started = time.monotonic()
        uploaded_bytes = 0

        # Update status before sending
        attempt_str = f" (urinish {attempt})" if attempt > 1 else ""
        await safe_edit(
            status_message,
            f"📤 Telegramga yuborilmoqda{attempt_str}...\n"
            f"Fayl: {file_name}\n"
            f"Hajm: {format_bytes(download_result.total_size)}\n"
            f"Tur: {kind}",
        )

        # Start a background progress updater during upload
        progress_task = asyncio.create_task(
            self._upload_progress_loop(
                status_message=status_message,
                file_name=file_name,
                file_size=download_result.total_size,
                kind=kind,
                started_at=send_started,
                uploaded_bytes_getter=lambda: uploaded_bytes,
            )
        )

        try:

            def update_uploaded_bytes(value: int) -> None:
                nonlocal uploaded_bytes
                uploaded_bytes = value

            input_file = ProgressFSInputFile(
                path=file_path,
                filename=file_name,
                chunk_size=upload_chunk_bytes,
                progress_callback=update_uploaded_bytes,
            )
            await self._do_send(message, input_file, caption, kind, download_result)
        finally:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task

        elapsed = max(1e-3, time.monotonic() - send_started)
        avg_speed = download_result.total_size / elapsed
        self.logger.info(
            "Sent '%s' as %s in %.1fs @ %s/s",
            file_name,
            kind,
            elapsed,
            format_bytes(int(avg_speed)),
        )
        return SendReport(
            elapsed_seconds=elapsed,
            avg_speed_bytes_per_second=avg_speed,
            sent_as=kind,
        )

    async def _do_send(
        self,
        message: Message,
        input_file: FSInputFile,
        caption: str,
        kind: str,
        download_result: DownloadResult,
    ) -> None:
        """Dispatch to the correct Telegram send method based on kind."""
        parse_mode = "HTML"

        if kind == "video":
            await message.answer_video(
                video=input_file,
                caption=caption,
                parse_mode=parse_mode,
                supports_streaming=True,
            )
        elif kind == "audio":
            await message.answer_audio(
                audio=input_file,
                caption=caption,
                parse_mode=parse_mode,
            )
        elif kind == "photo":
            # Photos have strict 10 MB limit; fall back to document if larger
            if download_result.total_size > 10 * 1024 * 1024:
                await message.answer_document(
                    document=input_file,
                    caption=caption,
                    parse_mode=parse_mode,
                )
            else:
                await message.answer_photo(
                    photo=input_file,
                    caption=caption,
                    parse_mode=parse_mode,
                )
        else:
            await message.answer_document(
                document=input_file,
                caption=caption,
                parse_mode=parse_mode,
            )

    async def _upload_progress_loop(
        self,
        status_message: Message,
        file_name: str,
        file_size: int,
        kind: str,
        started_at: float,
        uploaded_bytes_getter: Callable[[], int],
    ) -> None:
        """Periodically update the status message while uploading."""
        interval = self.config.send_progress_interval_seconds
        spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        tick = 0
        try:
            while True:
                await asyncio.sleep(interval)
                elapsed = max(1e-3, time.monotonic() - started_at)
                spinner = spinners[tick % len(spinners)]
                tick += 1
                uploaded = min(file_size, max(0, uploaded_bytes_getter()))
                speed = uploaded / elapsed
                percent = (uploaded / file_size * 100) if file_size > 0 else 0.0
                remaining = max(0, file_size - uploaded)
                eta = remaining / speed if speed > 0 else 0
                await safe_edit(
                    status_message,
                    f"{spinner} Telegramga yuborilmoqda...\n"
                    f"Fayl: {file_name}\n"
                    f"Progress: {format_bytes(uploaded)} / {format_bytes(file_size)} ({percent:.1f}%)\n"
                    f"Tezlik: {format_bytes(int(speed))}/s | ETA: {format_duration(eta)} | Tur: {kind}",
                )
        except asyncio.CancelledError:
            pass

    # ──────────────────────────────────────────────────────────────────────
    # YOUTUBE DOWNLOADER
    # ──────────────────────────────────────────────────────────────────────

    def _youtube_format_selector(self) -> str:
        max_bytes = max(1, self.config.max_file_bytes)
        if self.config.youtube_prefer_mp4:
            return (
                f"best[ext=mp4][vcodec!=none][acodec!=none][filesize<{max_bytes}]"
                f"/best[vcodec!=none][acodec!=none][filesize<{max_bytes}]"
                "/best[ext=mp4][vcodec!=none][acodec!=none]"
                "/best[vcodec!=none][acodec!=none]"
                "/best"
            )
        return (
            f"best[vcodec!=none][acodec!=none][filesize<{max_bytes}]"
            "/best[vcodec!=none][acodec!=none]"
            "/best"
        )

    async def download_youtube_file(
        self, url: str, status_message: Message
    ) -> DownloadResult:
        if not self.config.youtube_enabled:
            raise AppError(
                "YouTube downloader o'chirilgan. `YOUTUBE_ENABLED=true` qilib yoqing."
            )
        if yt_dlp is None:
            raise AppError(
                "YouTube downloader uchun `yt-dlp` o'rnatilmagan. `pip install -r requirements.txt` bajaring."
            )

        output_prefix = self.config.temp_dir / f"yt_{uuid.uuid4().hex}"
        progress_lock = threading.Lock()
        progress_state: dict[str, object] = {
            "status": "starting",
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "speed": 0.0,
            "eta": 0.0,
            "filename": "",
        }

        cookies_file = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
        resolved_cookies_file: Optional[str] = None
        if cookies_file:
            cookie_path = resolve_app_path(cookies_file, cookies_file)
            if cookie_path.exists():
                resolved_cookies_file = str(cookie_path)
            else:
                self.logger.warning("YOUTUBE_COOKIES_FILE topilmadi: %s", cookie_path)

        def progress_hook(data: dict) -> None:
            status = str(data.get("status") or "")
            downloaded_bytes = int(data.get("downloaded_bytes") or 0)
            total_bytes_raw = (
                data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            )
            total_bytes = int(total_bytes_raw) if total_bytes_raw else 0
            speed = float(data.get("speed") or 0.0)
            eta = float(data.get("eta") or 0.0)
            filename = data.get("filename")
            with progress_lock:
                progress_state["status"] = status or "downloading"
                progress_state["downloaded_bytes"] = downloaded_bytes
                progress_state["total_bytes"] = total_bytes
                progress_state["speed"] = speed
                progress_state["eta"] = eta
                if filename:
                    progress_state["filename"] = Path(str(filename)).name

        def run_youtube_download() -> DownloadResult:
            if yt_dlp is None:
                raise AppError("YouTube downloader uchun `yt-dlp` o'rnatilmagan.")

            common_opts: dict[str, object] = {
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": self.config.read_timeout_seconds,
                "retries": self.config.max_retries,
                "fragment_retries": self.config.max_retries,
                "ignoreerrors": False,
                "http_headers": {"User-Agent": self.config.user_agent},
            }
            if resolved_cookies_file:
                common_opts["cookiefile"] = resolved_cookies_file

            with yt_dlp.YoutubeDL(common_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            if not isinstance(info, dict):
                raise AppError("YouTube video ma'lumotini olishda xatolik yuz berdi.")

            if info.get("_type") == "playlist":
                entries = info.get("entries") or []
                if not entries:
                    raise AppError("YouTube playlist bo'sh yoki ochiq emas.")
                first_entry = entries[0]
                if not isinstance(first_entry, dict):
                    raise AppError("YouTube playlistdan video topilmadi.")
                info = first_entry

            if info.get("is_live"):
                raise AppError(
                    "Live stream qo'llab-quvvatlanmaydi. Oddiy video yuboring."
                )

            duration = info.get("duration")
            if (
                self.config.youtube_max_duration_seconds > 0
                and isinstance(duration, (int, float))
                and int(duration) > self.config.youtube_max_duration_seconds
            ):
                raise AppError(
                    "Video juda uzun. "
                    f"Maksimum: {format_duration(self.config.youtube_max_duration_seconds)}."
                )

            estimated_size = info.get("filesize") or info.get("filesize_approx")
            if (
                isinstance(estimated_size, (int, float))
                and int(estimated_size) > self.config.max_file_bytes
            ):
                raise FileTooLargeError(
                    f"Video juda katta: {format_bytes(int(estimated_size))}. "
                    f"Maksimum: {format_bytes(self.config.max_file_bytes)}."
                )

            ydl_opts: dict[str, object] = dict(common_opts)
            ydl_opts.update(
                {
                    "outtmpl": str(output_prefix) + ".%(ext)s",
                    "format": self._youtube_format_selector(),
                    "progress_hooks": [progress_hook],
                    "concurrent_fragment_downloads": 2,
                }
            )

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(url, download=True)
                if not isinstance(result, dict):
                    raise AppError("YouTube dan fayl olishda xatolik yuz berdi.")

                candidate_paths: list[Path] = []
                requested_downloads = result.get("requested_downloads")
                if isinstance(requested_downloads, list):
                    for item in requested_downloads:
                        if not isinstance(item, dict):
                            continue
                        requested_path = item.get("filepath") or item.get("filename")
                        if requested_path:
                            candidate_paths.append(Path(str(requested_path)))

                with contextlib.suppress(Exception):
                    prepared = ydl.prepare_filename(result)
                    if prepared:
                        candidate_paths.append(Path(prepared))

                for file_path in self.config.temp_dir.glob(f"{output_prefix.name}.*"):
                    if file_path.suffix.lower() in {".part", ".ytdl", ".tmp"}:
                        continue
                    candidate_paths.append(file_path)

                seen: set[str] = set()
                final_path: Optional[Path] = None
                for candidate in candidate_paths:
                    key = str(candidate)
                    if key in seen:
                        continue
                    seen.add(key)
                    if candidate.exists() and candidate.is_file():
                        final_path = candidate
                        break

                if final_path is None:
                    raise AppError("YouTube faylini topib bo'lmadi.")

                size_bytes = final_path.stat().st_size
                if size_bytes <= 0:
                    raise AppError("YouTube fayli bo'sh qaytdi.")
                if size_bytes > self.config.max_file_bytes:
                    raise FileTooLargeError(
                        f"Video limitdan katta: {format_bytes(size_bytes)} > "
                        f"{format_bytes(self.config.max_file_bytes)}."
                    )

                title = str(result.get("title") or info.get("title") or final_path.stem)
                suffix = final_path.suffix or ".mp4"
                output_filename = sanitize_file_name(f"{title}{suffix}")
                content_type = (
                    mimetypes.guess_type(final_path.name)[0]
                    or "application/octet-stream"
                )
                return DownloadResult(
                    temp_path=final_path,
                    output_filename=output_filename,
                    total_size=size_bytes,
                    content_type=content_type,
                )

        async def cleanup_sidecars() -> None:
            for pattern in (
                f"{output_prefix.name}*.part",
                f"{output_prefix.name}*.ytdl",
                f"{output_prefix.name}*.tmp",
            ):
                for file_path in self.config.temp_dir.glob(pattern):
                    await self.safe_remove_temp_file(file_path)

        download_task = asyncio.create_task(asyncio.to_thread(run_youtube_download))
        last_progress = ""
        try:
            while not download_task.done():
                await asyncio.sleep(self.config.progress_interval_seconds)
                with progress_lock:
                    status = str(progress_state.get("status") or "starting")
                    downloaded = int(progress_state.get("downloaded_bytes") or 0)
                    total = int(progress_state.get("total_bytes") or 0)
                    speed = float(progress_state.get("speed") or 0.0)
                    eta = float(progress_state.get("eta") or 0.0)
                    current_name = str(
                        progress_state.get("filename") or "YouTube media"
                    )

                if status == "downloading":
                    if total > 0:
                        percent = downloaded / total * 100
                        progress_text = (
                            f"🎬 YouTube yuklanmoqda: {current_name}\n"
                            f"Progress: {format_bytes(downloaded)} / {format_bytes(total)} ({percent:.1f}%)\n"
                            f"Tezlik: {format_bytes(int(speed))}/s | ETA: {format_duration(eta)}"
                        )
                    else:
                        progress_text = (
                            f"🎬 YouTube yuklanmoqda: {current_name}\n"
                            f"Progress: {format_bytes(downloaded)}\n"
                            f"Tezlik: {format_bytes(int(speed))}/s"
                        )
                elif status == "finished":
                    progress_text = (
                        "✅ YouTube fayl yuklandi. Telegramga tayyorlanmoqda..."
                    )
                else:
                    progress_text = "🔎 YouTube link tekshirilmoqda..."

                if progress_text != last_progress:
                    await safe_edit(status_message, progress_text)
                    last_progress = progress_text

            result = await download_task
            return result
        except asyncio.CancelledError:
            download_task.cancel()
            raise
        except FileTooLargeError:
            raise
        except AppError:
            raise
        except Exception as error:
            self.logger.exception("YouTube yuklashda xatolik")
            text = str(error).lower()
            if "unsupported url" in text:
                raise AppError("Bu YouTube link qo'llab-quvvatlanmaydi.")
            if "private video" in text or "video unavailable" in text:
                raise AppError("YouTube video mavjud emas yoki yopiq.")
            raise DownloadNetworkError(
                "YouTube yuklashda xatolik yuz berdi. Keyinroq qayta urinib ko'ring."
            )
        finally:
            await cleanup_sidecars()

    # ──────────────────────────────────────────────────────────────────────
    # DOWNLOADING
    # ──────────────────────────────────────────────────────────────────────

    async def download_file(self, url: str, status_message: Message) -> DownloadResult:
        if self.http_session is None:
            raise RuntimeError("HTTP session tayyor emas.")

        last_error: Optional[Exception] = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                return await self._download_once(url, status_message)
            except TransientDownloadError as error:
                last_error = error
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                asyncio.TimeoutError,
            ) as error:
                last_error = error
            except asyncio.CancelledError:
                raise

            if attempt >= self.config.max_retries:
                break

            delay_seconds = retry_delay(self.config.retry_backoff_seconds, attempt)
            await safe_edit(
                status_message,
                f"⚠️ Tarmoq xatosi. {delay_seconds:.0f}s da qayta uriniladi ({attempt}/{self.config.max_retries}).",
            )
            await asyncio.sleep(delay_seconds)

        self.logger.warning("Download retry limit reached: %s", last_error)
        raise DownloadNetworkError(
            "Tarmoq barqaror emas yoki server javob bermayapti. Keyinroq qayta urinib ko'ring."
        )

    async def _download_once(self, url: str, status_message: Message) -> DownloadResult:
        if self.http_session is None:
            raise RuntimeError("HTTP session tayyor emas.")

        headers = self._request_headers()
        async with self.http_session.get(
            url, allow_redirects=True, headers=headers
        ) as response:
            if response.status in TRANSIENT_HTTP_CODES:
                raise TransientDownloadError(
                    f"HTTP transient status: {response.status}"
                )
            if response.status == 403:
                raise AccessDeniedError("Server kirish taqiqladi (403 Forbidden).")
            if response.status == 404:
                raise AppError("Fayl topilmadi (404 Not Found).")
            if response.status >= 400:
                raise AppError(f"Server xatosi: HTTP {response.status}")

            if not looks_like_direct_link(response.headers):
                raise NonDirectLinkError(
                    "Bu direct fayl link emas. HTML sahifa qaytdi."
                )

            content_length = parse_content_length(response.headers)
            content_type = (
                str(response.headers.get("Content-Type", ""))
                .split(";", 1)[0]
                .strip()
                .lower()
            )

            if content_length and content_length > self.config.max_file_bytes:
                raise FileTooLargeError(
                    f"Fayl juda katta: {format_bytes(content_length)}. "
                    f"Maksimum: {format_bytes(self.config.max_file_bytes)}."
                )

            output_filename = filename_from_headers(str(response.url), response.headers)
            temp_file_path = self.config.temp_dir / f"{uuid.uuid4().hex}.part"

            use_parallel = (
                self.config.download_workers >= 2
                and content_length is not None
                and content_length
                >= max(
                    MIN_PARALLEL_DOWNLOAD_BYTES, self.config.parallel_download_min_bytes
                )
                and supports_range_download(response.headers)
            )

            if use_parallel:
                try:
                    return await self._download_parallel_streams(
                        url=str(response.url),
                        request_headers=headers,
                        temp_file_path=temp_file_path,
                        output_filename=output_filename,
                        content_length=content_length,
                        content_type=content_type,
                        status_message=status_message,
                    )
                except RangeUnsupportedError:
                    self.logger.warning(
                        "Server Range qo'llab-quvvatlamadi. Single streamga o'tildi."
                    )
                    await self.safe_remove_temp_file(temp_file_path)

            return await self._download_single_stream(
                response=response,
                temp_file_path=temp_file_path,
                output_filename=output_filename,
                status_message=status_message,
                content_length=content_length,
                content_type=content_type,
            )

    async def _download_single_stream(
        self,
        response: aiohttp.ClientResponse,
        temp_file_path: Path,
        output_filename: str,
        status_message: Message,
        content_length: Optional[int],
        content_type: str,
    ) -> DownloadResult:
        downloaded = 0
        start_time = time.monotonic()
        last_progress_update = 0.0
        pending_status_update: Optional[asyncio.Task] = None
        chunk_bytes = self.config.chunk_size_kb * 1024

        try:
            with temp_file_path.open(
                "wb", buffering=FILE_IO_BUFFER_BYTES
            ) as output_file:
                async for chunk in response.content.iter_chunked(chunk_bytes):
                    if not chunk:
                        continue
                    output_file.write(chunk)
                    downloaded += len(chunk)

                    if downloaded > self.config.max_file_bytes:
                        raise FileTooLargeError(
                            f"Fayl limitdan oshdi ({format_bytes(downloaded)} > "
                            f"{format_bytes(self.config.max_file_bytes)})."
                        )

                    now = time.monotonic()
                    if (
                        now - last_progress_update
                        < self.config.progress_interval_seconds
                    ):
                        continue

                    elapsed = max(1e-3, now - start_time)
                    speed = downloaded / elapsed
                    speed_text = format_bytes(int(speed))
                    if content_length:
                        percent = downloaded / content_length * 100
                        remaining = max(0, content_length - downloaded)
                        eta = remaining / speed if speed > 0 else 0
                        progress = (
                            f"⬇️ Yuklanmoqda: {format_bytes(downloaded)} / {format_bytes(content_length)} "
                            f"({percent:.1f}%) | {speed_text}/s | ETA {format_duration(eta)}"
                        )
                    else:
                        progress = (
                            f"⬇️ Yuklanmoqda: {format_bytes(downloaded)} | "
                            f"{speed_text}/s | {format_duration(int(elapsed))}"
                        )

                    if pending_status_update is None or pending_status_update.done():
                        pending_status_update = asyncio.create_task(
                            safe_edit(status_message, progress)
                        )
                    last_progress_update = now

            if pending_status_update is not None:
                with contextlib.suppress(Exception):
                    await pending_status_update

            if downloaded <= 0:
                raise AppError("Bo'sh fayl qaytdi. Bu direct link bo'lmasligi mumkin.")

            return DownloadResult(
                temp_path=temp_file_path,
                output_filename=output_filename,
                total_size=downloaded,
                content_type=content_type,
            )
        except Exception:
            if pending_status_update is not None:
                with contextlib.suppress(Exception):
                    await pending_status_update
            await self.safe_remove_temp_file(temp_file_path)
            raise

    async def _download_parallel_streams(
        self,
        url: str,
        request_headers: dict[str, str],
        temp_file_path: Path,
        output_filename: str,
        content_length: int,
        content_type: str,
        status_message: Message,
    ) -> DownloadResult:
        """
        Parallel download with FIXED:
        - asyncio.Lock for file seek+write (prevents race condition / file corruption)
        - asyncio.Lock for shared `downloaded` counter
        - Correct segment verification
        """
        if self.http_session is None:
            raise RuntimeError("HTTP session tayyor emas.")
        if content_length <= 0:
            raise AppError("Parallel yuklash uchun Content-Length aniqlanmadi.")

        worker_count = min(
            self.config.download_workers, max(1, content_length // (8 * 1024 * 1024))
        )
        worker_count = max(2, worker_count)
        ranges = build_ranges(content_length, worker_count)
        if len(ranges) < 2:
            raise AppError("Parallel yuklash uchun range segmentlar hosil bo'lmadi.")

        downloaded = 0
        start_time = time.monotonic()
        last_progress_update = 0.0
        pending_status_update: Optional[asyncio.Task] = None
        chunk_bytes = self.config.chunk_size_kb * 1024

        # FIX: Lock to prevent concurrent seek+write race conditions
        file_write_lock = asyncio.Lock()
        # FIX: Lock for shared counter
        counter_lock = asyncio.Lock()

        async def worker(start: int, end: int) -> None:
            nonlocal downloaded, last_progress_update, pending_status_update
            range_headers = {**request_headers, "Range": f"bytes={start}-{end}"}
            written = 0

            async with self.http_session.get(
                url, allow_redirects=True, headers=range_headers
            ) as response:
                if response.status in TRANSIENT_HTTP_CODES:
                    raise TransientDownloadError(
                        f"Parallel HTTP transient status: {response.status}"
                    )
                if response.status not in {200, 206}:
                    raise AppError(
                        f"Parallel yuklashda server xatosi: HTTP {response.status}"
                    )
                if response.status == 200 and (start != 0 or end != content_length - 1):
                    raise RangeUnsupportedError(
                        "Server Range qo'llab-quvvatlamaydi. Parallel rejim bekor qilindi."
                    )

                pos = start
                async for chunk in response.content.iter_chunked(chunk_bytes):
                    if not chunk:
                        continue
                    chunk_len = len(chunk)

                    # FIX: Lock file write to prevent seek+write race condition
                    async with file_write_lock:
                        with temp_file_path.open("r+b", buffering=0) as output_file:
                            output_file.seek(pos)
                            output_file.write(chunk)

                    pos += chunk_len
                    written += chunk_len

                    async with counter_lock:
                        downloaded += chunk_len
                        current_downloaded = downloaded

                    if current_downloaded > self.config.max_file_bytes:
                        raise FileTooLargeError(
                            f"Fayl limitdan oshdi ({format_bytes(current_downloaded)} > "
                            f"{format_bytes(self.config.max_file_bytes)})."
                        )

                    now = time.monotonic()
                    if (
                        now - last_progress_update
                        >= self.config.progress_interval_seconds
                    ):
                        elapsed = max(1e-3, now - start_time)
                        speed = current_downloaded / elapsed
                        percent = current_downloaded / content_length * 100
                        remaining = max(0, content_length - current_downloaded)
                        eta = remaining / speed if speed > 0 else 0
                        progress_text = (
                            f"⬇️ Yuklanmoqda (x{worker_count}): "
                            f"{format_bytes(current_downloaded)} / {format_bytes(content_length)} "
                            f"({percent:.1f}%) | {format_bytes(int(speed))}/s | ETA {format_duration(eta)}"
                        )
                        if (
                            pending_status_update is None
                            or pending_status_update.done()
                        ):
                            pending_status_update = asyncio.create_task(
                                safe_edit(status_message, progress_text)
                            )
                        last_progress_update = now

            # Verify we got exactly the expected bytes for this segment
            expected = end - start + 1
            if response.status == 206 and written != expected:
                raise DownloadNetworkError(
                    f"Parallel yuklashda segment to'liq olinmadi: "
                    f"kutilgan {expected}B, olingan {written}B."
                )

        try:
            # Pre-allocate the file
            with temp_file_path.open("wb") as output_file:
                output_file.truncate(content_length)

            tasks = [asyncio.create_task(worker(start, end)) for start, end in ranges]
            done = await asyncio.gather(*tasks, return_exceptions=True)

            # Collect and raise errors
            errors = [item for item in done if isinstance(item, BaseException)]
            if errors:
                raise errors[0]

            if pending_status_update is not None:
                with contextlib.suppress(Exception):
                    await pending_status_update

            # Verify total downloaded matches expected
            async with counter_lock:
                final_downloaded = downloaded

            if final_downloaded <= 0:
                raise AppError("Bo'sh fayl qaytdi. Bu direct link bo'lmasligi mumkin.")

            # Verify file size on disk
            actual_size = temp_file_path.stat().st_size
            if actual_size != content_length:
                raise DownloadNetworkError(
                    f"Fayl hajmi mos emas: disk={format_bytes(actual_size)}, "
                    f"kutilgan={format_bytes(content_length)}."
                )

            return DownloadResult(
                temp_path=temp_file_path,
                output_filename=output_filename,
                total_size=final_downloaded,
                content_type=content_type,
            )
        except Exception:
            if pending_status_update is not None:
                with contextlib.suppress(Exception):
                    await pending_status_update
            await self.safe_remove_temp_file(temp_file_path)
            raise

    # ──────────────────────────────────────────────────────────────────────
    # MAIN HANDLER
    # ──────────────────────────────────────────────────────────────────────

    async def handle_link(self, message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        if not self.user_has_access(user_id):
            await message.answer("❌ Sizda bu botdan foydalanish ruxsati yo'q.")
            return

        raw_url = extract_url(message)
        if not raw_url:
            await message.answer(
                "🔗 Direct download link yuboring.\nMasalan: https://example.com/file.zip"
            )
            return

        try:
            url = normalize_url(raw_url, self.config.max_url_length)
        except AppError as error:
            await message.answer(f"❌ {error.user_message}")
            return

        user_slot_ok = await self.acquire_user_slot(user_id)
        if not user_slot_ok:
            await message.answer(
                "⏳ Avvalgi yuklash tugasin, keyin yangi link yuboring."
            )
            return

        status_message = await message.answer(
            "🔍 Link qabul qilindi. Tekshirilmoqda..."
        )
        download_result: Optional[DownloadResult] = None
        current_task = asyncio.current_task()
        if current_task is not None:
            await self.set_user_task(user_id, current_task)

        async with self.download_semaphore:
            try:
                # ── Download ─────────────────────────────────────────────
                youtube_mode = is_youtube_url(url)
                if youtube_mode and not self.config.youtube_enabled:
                    raise AppError("YouTube downloader hozir o'chirilgan.")

                overall_download_timeout = (
                    self.config.youtube_timeout_seconds
                    if youtube_mode
                    else max(
                        180,
                        self.config.read_timeout_seconds
                        * max(1, self.config.max_retries)
                        + 60,
                    )
                )
                if youtube_mode:
                    await safe_edit(
                        status_message,
                        "🎬 YouTube link aniqlandi. Yuklash boshlandi...",
                    )
                try:
                    download_result = await asyncio.wait_for(
                        self.download_youtube_file(url, status_message)
                        if youtube_mode
                        else self.download_file(url, status_message),
                        timeout=overall_download_timeout,
                    )
                except asyncio.TimeoutError as error:
                    raise DownloadNetworkError(
                        f"⏳ {'YouTube' if youtube_mode else 'Yuklash'} vaqti tugadi ({overall_download_timeout}s). "
                        "Keyinroq qayta urinib ko'ring."
                    ) from error

                await safe_edit(
                    status_message,
                    f"✅ Yuklash tugadi ({format_bytes(download_result.total_size)}). "
                    "Telegramga yuborilmoqda...",
                )

                # ── Send ─────────────────────────────────────────────────
                send_report = await self._send_download_result(
                    message, status_message, download_result
                )
                await safe_edit(
                    status_message,
                    (
                        f"✅ Tayyor! Fayl yuborildi ({send_report.sent_as}).\n"
                        f"⏱ Jo'natish: {format_duration(send_report.elapsed_seconds)} | "
                        f"🚀 Tezlik: {format_bytes(int(send_report.avg_speed_bytes_per_second))}/s"
                    ),
                )
                self.completed_downloads += 1
                self.total_downloaded_bytes += download_result.total_size

            except asyncio.CancelledError:
                self.failed_downloads += 1
                await safe_edit(status_message, "🚫 Yuklash bekor qilindi.")
            except (aiohttp.InvalidURL, InvalidLinkError) as e:
                self.failed_downloads += 1
                await safe_edit(status_message, f"❌ URL noto'g'ri: {e}")
            except NonDirectLinkError as e:
                self.failed_downloads += 1
                await safe_edit(status_message, f"❌ {e.user_message}")
            except FileTooLargeError as e:
                self.failed_downloads += 1
                await safe_edit(status_message, f"❌ {e.user_message}")
            except AccessDeniedError as e:
                self.failed_downloads += 1
                await safe_edit(status_message, f"🔒 {e.user_message}")
            except DownloadNetworkError as e:
                self.failed_downloads += 1
                await safe_edit(status_message, f"⚠️ {e.user_message}")
            except SendFailedError as e:
                self.failed_downloads += 1
                await safe_edit(status_message, f"❌ {e.user_message}")
            except AppError as e:
                self.failed_downloads += 1
                await safe_edit(status_message, f"❌ {e.user_message}")
            except TelegramBadRequest as e:
                self.failed_downloads += 1
                await safe_edit(
                    status_message,
                    telegram_error_message(e, self.config.max_file_bytes),
                )
            except TelegramRetryAfter as e:
                self.failed_downloads += 1
                await safe_edit(
                    status_message,
                    f"⏳ Telegram limit. {int(e.retry_after)}s dan keyin qayta urinib ko'ring.",
                )
            except TelegramNetworkError as e:
                self.failed_downloads += 1
                self.logger.warning("Telegram send tarmoq xatosi: %s", e)
                await safe_edit(
                    status_message,
                    "⚠️ Telegram bilan aloqa uzildi. Keyinroq qayta urinib ko'ring.",
                )
            except (aiohttp.ClientError, asyncio.TimeoutError):
                self.failed_downloads += 1
                await safe_edit(
                    status_message,
                    "⚠️ Tarmoq xatosi yuz berdi. Keyinroq qayta urinib ko'ring.",
                )
            except PermissionError:
                self.failed_downloads += 1
                await safe_edit(status_message, "❌ Diskga yozishda ruxsat xatosi.")
            except OSError as e:
                self.failed_downloads += 1
                self.logger.exception("Disk xatosi")
                await safe_edit(status_message, f"❌ Disk xatosi: {e}")
            except Exception as e:
                self.failed_downloads += 1
                self.logger.exception("Kutilmagan xatolik")
                await safe_edit(
                    status_message, f"❌ Kutilmagan xatolik: {type(e).__name__}: {e}"
                )
            finally:
                await self.release_user_slot(user_id)
                if current_task is not None:
                    await self.clear_user_task(user_id, current_task)
                if download_result is not None:
                    await self.safe_remove_temp_file(download_result.temp_path)


# ──────────────────────────────────────────────
# Bot setup
# ──────────────────────────────────────────────


def build_bot_session(config: Config) -> AiohttpSession:
    # aiogram expects BaseSession.timeout to be numeric (seconds), not aiohttp.ClientTimeout.
    upload_timeout_seconds = float(
        max(
            300,
            config.upload_timeout_seconds,
            config.read_timeout_seconds * 3,
        )
    )
    connector_limit = max(100, min(config.http_connector_limit, 500))

    if config.bot_api_base:
        parsed = urlsplit(config.bot_api_base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("BOT_API_BASE noto'g'ri. Misol: http://127.0.0.1:8081")
        api = TelegramAPIServer.from_base(config.bot_api_base)
        return AiohttpSession(
            api=api, timeout=upload_timeout_seconds, limit=connector_limit
        )

    return AiohttpSession(timeout=upload_timeout_seconds, limit=connector_limit)


def register_handlers(
    dispatcher: Dispatcher, app: DirectLinkDownloaderBot, config: Config
) -> None:

    @dispatcher.message(CommandStart())
    async def start_handler(message: Message) -> None:
        text = (
            "👋 <b>Direct Link Downloader Bot</b>\n\n"
            "Direct link yoki YouTube link yuboring — fayl yuklab Telegramga yuboriladi.\n\n"
            f"📦 Limit: <b>{format_bytes(config.max_file_bytes)}</b>\n"
            "✅ Qo'llab-quvvatlanadi: direct HTTP/HTTPS va YouTube\n\n"
            "<b>Buyruqlar:</b>\n"
            "/help — yordam\n"
            "/limits — limitlar\n"
            "/process — jarayon holati\n"
            "/stats — statistika\n"
            "/cancel — joriy yuklashni bekor qilish"
        )
        await message.answer(text, parse_mode="HTML")

    @dispatcher.message(Command("help"))
    async def help_handler(message: Message) -> None:
        text = (
            "<b>Ishlash tartibi:</b>\n"
            "1. Direct yoki YouTube link yuboring\n"
            "2. Bot faylni yuklab oladi\n"
            "3. Faylni Telegramga yuboradi\n\n"
            "<b>Buyruqlar:</b>\n"
            "/start — boshlash\n"
            "/help — yordam\n"
            "/limits — limitlar\n"
            "/process — jarayon holati\n"
            "/stats — ish statistikasi\n"
            "/cancel — joriy yuklashni bekor qilish\n\n"
            "<b>Eslatma:</b> YouTube yoki direct fayl link yuboring."
        )
        await message.answer(text, parse_mode="HTML")

    @dispatcher.message(Command("limits"))
    async def limits_handler(message: Message) -> None:
        allowed_mode = (
            "Hamma foydalanuvchi"
            if not config.allowed_user_ids
            else "Faqat ruxsat berilgan ID lar"
        )
        speed_mode = "dual-worker" if config.download_workers >= 2 else "single-worker"
        text = (
            f"📦 Fayl limiti: {format_bytes(config.max_file_bytes)}\n"
            f"⚡ Global parallel: {config.concurrent_downloads}\n"
            f"👤 Per-user limit: {config.per_user_download_limit}\n"
            f"🔄 Download workers: {config.download_workers} ({speed_mode})\n"
            f"📐 Parallel min size: {format_bytes(config.parallel_download_min_bytes)}\n"
            f"🔗 Connector: {config.http_connector_limit}/{config.http_connector_limit_per_host}\n"
            f"📤 Upload chunk: {config.upload_chunk_kb} KB\n"
            f"⏱ Upload timeout: {format_duration(config.upload_timeout_seconds)}\n"
            f"🔁 Send retry: {config.send_max_retries} marta\n"
            f"🎬 YouTube: {'yoqilgan' if config.youtube_enabled else 'o‘chirilgan'}\n"
            f"⏳ YouTube timeout: {format_duration(config.youtube_timeout_seconds)}\n"
            f"🔒 Kirish: {allowed_mode}"
        )
        await message.answer(text)

    @dispatcher.message(Command("process"))
    async def process_handler(message: Message) -> None:
        await message.answer(app.build_process_snapshot())

    @dispatcher.message(Command("stats"))
    async def stats_handler(message: Message) -> None:
        text = (
            f"✅ Tugallangan: {app.completed_downloads}\n"
            f"❌ Xatoliklar: {app.failed_downloads}\n"
            f"📦 Jami yuklangan: {format_bytes(app.total_downloaded_bytes)}\n"
            f"👥 Faol foydalanuvchilar: {len(app.active_per_user)}"
        )
        await message.answer(text)

    @dispatcher.message(Command("cancel"))
    async def cancel_handler(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else None
        cancelled = await app.cancel_user_task(user_id)
        if cancelled:
            await message.answer("🚫 Joriy yuklash bekor qilindi.")
        else:
            await message.answer("ℹ️ Bekor qilish uchun faol yuklash topilmadi.")

    @dispatcher.message(F.text | F.caption)
    async def text_handler(message: Message) -> None:
        if message.text and message.text.lstrip().startswith("/"):
            return
        await app.handle_link(message)


# ──────────────────────────────────────────────
# Polling
# ──────────────────────────────────────────────


async def run_polling_forever(dispatcher: Dispatcher, bot: Bot, config: Config) -> None:
    logger = logging.getLogger("Polling")
    while True:
        try:
            await dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
            logger.info("Polling normal to'xtadi.")
            break
        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt qabul qilindi.")
            raise
        except TelegramUnauthorizedError:
            logger.critical(
                "BOT_TOKEN noto'g'ri yoki revoke qilingan. Retry to'xtatildi."
            )
            break
        except TelegramConflictError:
            logger.error(
                "Conflict: boshqa getUpdates jarayoni mavjud. Yagona instance qoldiring."
            )
            await asyncio.sleep(config.polling_restart_delay_seconds)
            continue
        except (
            TelegramNetworkError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ) as error:
            logger.exception("Polling tarmoq xatosi: %s", error)
        except Exception:
            logger.exception("Pollingda kutilmagan xatolik")
        await asyncio.sleep(config.polling_restart_delay_seconds)


async def verify_bot_access(bot: Bot) -> None:
    logger = logging.getLogger("Startup")
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError as error:
        raise ValueError(
            "Telegram Unauthorized: BOT_TOKEN noto'g'ri yoki bekor qilingan."
        ) from error
    logger.info("Bot avtorizatsiya qilindi: @%s (id=%s)", me.username, me.id)


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────


async def main() -> None:
    try:
        config = load_config()
    except ValueError as error:
        logging.basicConfig(
            level=logging.ERROR,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
        logging.getLogger("Main").error(str(error))
        return

    setup_logging(config)
    logger = logging.getLogger("Main")
    lock = InstanceLock(config.lock_file)
    try:
        lock.acquire()
    except Exception as error:
        logger.error(str(error))
        return

    logger.info("Bot ishga tushmoqda")
    logger.info("Temp dir: %s", config.temp_dir.resolve())
    logger.info("Max size: %s", format_bytes(config.max_file_bytes))
    logger.info("Lock file: %s", config.lock_file.resolve())
    logger.info("Download workers: %s", config.download_workers)
    logger.info(
        "Connector limit: %s/%s",
        config.http_connector_limit,
        config.http_connector_limit_per_host,
    )
    logger.info("Upload chunk: %s KB", config.upload_chunk_kb)
    logger.info("Upload timeout: %ds", config.upload_timeout_seconds)
    logger.info("Send max retries: %d", config.send_max_retries)
    logger.info("YouTube enabled: %s", config.youtube_enabled)
    logger.info("YouTube timeout: %ds", config.youtube_timeout_seconds)

    app = DirectLinkDownloaderBot(config)
    await app.startup()

    bot_session = build_bot_session(config)
    bot = Bot(token=config.bot_token, session=bot_session)
    dispatcher = Dispatcher()
    register_handlers(dispatcher, app, config)

    try:
        await verify_bot_access(bot)
        await run_polling_forever(dispatcher, bot, config)
    except ValueError as error:
        logger.critical(str(error))
    finally:
        await app.shutdown()
        await bot.session.close()
        lock.release()


if __name__ == "__main__":
    asyncio.run(main())
