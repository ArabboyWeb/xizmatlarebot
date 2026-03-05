import asyncio
from typing import Any

MAX_INPUT_BYTES = 18 * 1024 * 1024


def _remove_background_sync(image_bytes: bytes) -> bytes:
    try:
        from rembg import remove
    except Exception as error:  # noqa: BLE001
        raise RuntimeError(
            "rembg kutubxonasi topilmadi. requirements ni yangilang."
        ) from error

    output: Any = remove(image_bytes)
    if isinstance(output, bytes) and output:
        return output
    raise RuntimeError("Rembg fonni olib tashlashda bo'sh javob qaytardi.")


async def remove_background(image_bytes: bytes) -> bytes:
    if not image_bytes:
        raise ValueError("Rasm bo'sh.")
    if len(image_bytes) > MAX_INPUT_BYTES:
        raise ValueError("Rasm juda katta. Maksimal 18 MB.")
    return await asyncio.to_thread(_remove_background_sync, image_bytes)
