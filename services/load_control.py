from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class LoadControlBusyError(RuntimeError):
    pass


class LoadControlTimeoutError(TimeoutError):
    pass


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_CONCURRENCY_LIMITS = {
    "ai": max(1, _read_int("AI_CONCURRENCY_LIMIT", 24)),
    "image": max(1, _read_int("IMAGE_CONCURRENCY_LIMIT", 8)),
    "download": max(1, _read_int("DOWNLOAD_CONCURRENCY_LIMIT", 6)),
    "converter": max(1, _read_int("CONVERTER_CONCURRENCY_LIMIT", 4)),
    "api": max(1, _read_int("API_CONCURRENCY_LIMIT", 16)),
}
_LIMITS = {name: asyncio.Semaphore(limit) for name, limit in _CONCURRENCY_LIMITS.items()}

_QUEUE_WAIT_TIMEOUTS = {
    "ai": max(1, _read_int("AI_QUEUE_WAIT_TIMEOUT_SECONDS", 20)),
    "image": max(1, _read_int("IMAGE_QUEUE_WAIT_TIMEOUT_SECONDS", 25)),
    "download": max(1, _read_int("DOWNLOAD_QUEUE_WAIT_TIMEOUT_SECONDS", 30)),
    "converter": max(1, _read_int("CONVERTER_QUEUE_WAIT_TIMEOUT_SECONDS", 30)),
    "api": max(1, _read_int("API_QUEUE_WAIT_TIMEOUT_SECONDS", 15)),
}
_EXECUTION_TIMEOUTS = {
    "ai": max(10, _read_int("AI_EXECUTION_TIMEOUT_SECONDS", 120)),
    "image": max(10, _read_int("IMAGE_EXECUTION_TIMEOUT_SECONDS", 90)),
    "download": max(0, _read_int("DOWNLOAD_EXECUTION_TIMEOUT_SECONDS", 0)),
    "converter": max(0, _read_int("CONVERTER_EXECUTION_TIMEOUT_SECONDS", 0)),
    "api": max(5, _read_int("API_EXECUTION_TIMEOUT_SECONDS", 30)),
}
_BUSY_MESSAGES = {
    "ai": "AI hozir band. Iltimos kuting va qayta urinib ko'ring.",
    "image": "Rasm yaratish navbatda. Iltimos kuting va qayta urinib ko'ring.",
    "download": "Yuklash navbatda. Iltimos kuting va qayta urinib ko'ring.",
    "converter": "Konvertatsiya navbatda. Iltimos kuting va qayta urinib ko'ring.",
    "api": "Servis hozir band. Iltimos kuting va qayta urinib ko'ring.",
}
_TIMEOUT_MESSAGES = {
    "ai": "AI javobi kechikdi. Iltimos birozdan keyin qayta urinib ko'ring.",
    "image": "Rasm yaratish vaqti tugadi. Iltimos qayta urinib ko'ring.",
    "download": "Yuklash vaqti tugadi. Iltimos qayta urinib ko'ring.",
    "converter": "Konvertatsiya vaqti tugadi. Iltimos qayta urinib ko'ring.",
    "api": "Servis javobi kechikdi. Iltimos qayta urinib ko'ring.",
}


async def _acquire(name: str) -> asyncio.Semaphore:
    semaphore = _LIMITS[name]
    wait_timeout = _QUEUE_WAIT_TIMEOUTS.get(name, 15)
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=wait_timeout)
    except asyncio.TimeoutError as error:
        raise LoadControlBusyError(_BUSY_MESSAGES.get(name, _BUSY_MESSAGES["api"])) from error
    return semaphore


async def _run_with_optional_timeout(name: str, callback: Callable[[], Awaitable[T]]) -> T:
    timeout_seconds = _EXECUTION_TIMEOUTS.get(name, 0)
    try:
        if timeout_seconds > 0:
            return await asyncio.wait_for(callback(), timeout=timeout_seconds)
        return await callback()
    except asyncio.TimeoutError as error:
        raise LoadControlTimeoutError(
            _TIMEOUT_MESSAGES.get(name, _TIMEOUT_MESSAGES["api"])
        ) from error


def limit_snapshot() -> dict[str, dict[str, int]]:
    return {
        name: {
            "concurrency_limit": _CONCURRENCY_LIMITS.get(name, 0),
            "queue_wait_timeout_seconds": _QUEUE_WAIT_TIMEOUTS.get(name, 0),
            "execution_timeout_seconds": _EXECUTION_TIMEOUTS.get(name, 0),
        }
        for name in _LIMITS
    }


async def run_with_limit(
    name: str,
    callback: Callable[[], Awaitable[T]],
) -> T:
    semaphore = await _acquire(name)
    try:
        return await _run_with_optional_timeout(name, callback)
    finally:
        semaphore.release()


async def run_in_thread_with_limit(
    name: str,
    callback: Callable[..., T],
    /,
    *args: object,
    **kwargs: object,
) -> T:
    semaphore = await _acquire(name)
    try:
        bound = functools.partial(callback, *args, **kwargs)
        return await _run_with_optional_timeout(name, lambda: asyncio.to_thread(bound))
    finally:
        semaphore.release()
