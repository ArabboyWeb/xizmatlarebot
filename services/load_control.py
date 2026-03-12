from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_LIMITS = {
    "ai": asyncio.Semaphore(max(1, _read_int("AI_CONCURRENCY_LIMIT", 24))),
    "image": asyncio.Semaphore(max(1, _read_int("IMAGE_CONCURRENCY_LIMIT", 8))),
    "download": asyncio.Semaphore(max(1, _read_int("DOWNLOAD_CONCURRENCY_LIMIT", 6))),
    "converter": asyncio.Semaphore(max(1, _read_int("CONVERTER_CONCURRENCY_LIMIT", 4))),
}


async def run_with_limit(
    name: str,
    callback: Callable[[], Awaitable[T]],
) -> T:
    semaphore = _LIMITS[name]
    async with semaphore:
        return await callback()


async def run_in_thread_with_limit(
    name: str,
    callback: Callable[..., T],
    /,
    *args: object,
    **kwargs: object,
) -> T:
    semaphore = _LIMITS[name]
    async with semaphore:
        bound = functools.partial(callback, *args, **kwargs)
        return await asyncio.to_thread(bound)
