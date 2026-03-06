import io
import os
from urllib.parse import quote

import aiohttp
from PIL import Image, ImageDraw

POLLINATIONS_BASE_URL = "https://image.pollinations.ai/prompt/"
HTTP_TIMEOUT_SECONDS = 60
MAX_PROMPT_LENGTH = 500
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


def _placeholder_image(prompt: str, width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), color=(18, 24, 38))
    draw = ImageDraw.Draw(image)
    draw.rectangle((16, 16, width - 16, height - 16), outline=(90, 120, 170), width=2)
    draw.text((28, 28), "Pollinations unavailable", fill=(220, 230, 255))
    draw.text((28, 58), "Prompt:", fill=(160, 180, 220))

    snippet = prompt[:240]
    line_length = max(24, min(56, width // 14))
    lines: list[str] = []
    while snippet:
        lines.append(snippet[:line_length])
        snippet = snippet[line_length:]
        if len(lines) >= 12:
            break
    text_y = 88
    for line in lines:
        draw.text((28, text_y), line, fill=(245, 245, 245))
        text_y += 22

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


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


async def generate_image(
    prompt: str,
    *,
    model: str = "flux",
    width: int = 1024,
    height: int = 1024,
    seed: int | None = None,
) -> bytes:
    clean_prompt = normalize_prompt(prompt)
    safe_model = (model or "flux").strip().lower() or "flux"
    w = int(width)
    h = int(height)
    if w < 256 or h < 256 or w > 1536 or h > 1536:
        raise ValueError("Rasm o'lchami 256-1536 oralig'ida bo'lishi kerak.")

    params: dict[str, str | int] = {
        "model": safe_model,
        "width": w,
        "height": h,
        "nologo": "true",
        "safe": "true",
    }
    if seed is not None:
        params["seed"] = int(seed)

    url = f"{POLLINATIONS_BASE_URL}{quote(clean_prompt, safe='')}"
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    headers = {
        "User-Agent": os.getenv("HTTP_USER_AGENT", "").strip() or DEFAULT_USER_AGENT,
        "Accept": "image/*,application/octet-stream,*/*",
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    content = await response.read()
                    normalized = _normalize_image_bytes(content)
                    if normalized:
                        return normalized
                    return _placeholder_image(clean_prompt, w, h)
                if response.status >= 500:
                    return _placeholder_image(clean_prompt, w, h)
                if response.status >= 400:
                    body = (await response.text())[:180]
                    raise RuntimeError(
                        f"Pollinations API xatosi: HTTP {response.status}. {body}"
                    )
    except (aiohttp.ClientError, TimeoutError):
        return _placeholder_image(clean_prompt, w, h)

    return _placeholder_image(clean_prompt, w, h)
