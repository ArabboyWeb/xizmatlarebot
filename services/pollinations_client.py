import asyncio
import io
import os
from urllib.parse import quote

import aiohttp
from PIL import Image

from services.load_control import run_with_limit

POLLINATIONS_BASE_URL = "https://image.pollinations.ai/prompt/"
HTTP_TIMEOUT_SECONDS = 60
MAX_PROMPT_LENGTH = 500
MAX_GENERATION_ATTEMPTS = 3
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (XizmatlarBot/1.0; +https://core.telegram.org/bots/api)"
)


def normalize_prompt(prompt: str) -> str:
    clean = (prompt or "").strip()
    if not clean:
        raise ValueError("Rasm yaratish uchun prompt yuboring.")
    if len(clean) > MAX_PROMPT_LENGTH:
        raise ValueError(f"Prompt juda uzun. Maksimal {MAX_PROMPT_LENGTH} belgi.")
    return clean


def _normalize_image_bytes(content: bytes) -> bytes:
    if not content:
        return b""
    try:
        with Image.open(io.BytesIO(content)) as image:
            prepared = image.convert("RGB")
            buffer = io.BytesIO()
            prepared.save(buffer, format="PNG")
            return buffer.getvalue()
    except Exception:
        return b""


def _model_candidates(selected_model: str) -> list[str]:
    safe_model = (selected_model or "flux").strip().lower() or "flux"
    fallbacks = ["flux", "turbo"]
    ordered = [safe_model]
    for item in fallbacks:
        if item not in ordered:
            ordered.append(item)
    return ordered


async def generate_image(
    prompt: str,
    *,
    model: str = "flux",
    width: int = 1024,
    height: int = 1024,
    seed: int | None = None,
) -> bytes:
    async def _run() -> bytes:
        clean_prompt = normalize_prompt(prompt)
        w = int(width)
        h = int(height)
        if w < 256 or h < 256 or w > 1536 or h > 1536:
            raise ValueError("Rasm o'lchami 256-1536 oralig'ida bo'lishi kerak.")

        url = f"{POLLINATIONS_BASE_URL}{quote(clean_prompt, safe='')}"
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        headers = {
            "User-Agent": os.getenv("HTTP_USER_AGENT", "").strip() or DEFAULT_USER_AGENT,
            "Accept": "image/*,application/octet-stream,*/*",
        }
        last_error: Exception | None = None
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for selected_model in _model_candidates(model):
                params: dict[str, str | int] = {
                    "model": selected_model,
                    "width": w,
                    "height": h,
                    "nologo": "true",
                    "safe": "true",
                    "enhance": "true",
                }
                if seed is not None:
                    params["seed"] = int(seed)

                for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
                    try:
                        async with session.get(url, params=params) as response:
                            if response.status == 200:
                                content = await response.read()
                                normalized = _normalize_image_bytes(content)
                                if normalized:
                                    return normalized
                                raise RuntimeError("Rasm bytes noto'g'ri qaytdi.")
                            if response.status >= 500 or response.status in {408, 429}:
                                raise RuntimeError(
                                    f"Rasm servisi javobi vaqtincha yaroqsiz: HTTP {response.status}"
                                )
                            raise RuntimeError("Rasm servisi vaqtincha ishlamayapti.")
                    except (aiohttp.ClientError, TimeoutError, RuntimeError) as error:
                        last_error = error
                        if attempt < MAX_GENERATION_ATTEMPTS:
                            await asyncio.sleep(1.2 * attempt)

        raise RuntimeError(
            "Rasm hozir yaratilmayapti. Birozdan keyin qayta urinib ko'ring."
        ) from last_error

    return await run_with_limit("image", _run)
