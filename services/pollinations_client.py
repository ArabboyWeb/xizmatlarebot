from urllib.parse import quote

import aiohttp

POLLINATIONS_BASE_URL = "https://image.pollinations.ai/prompt/"
HTTP_TIMEOUT_SECONDS = 60
MAX_PROMPT_LENGTH = 500


def normalize_prompt(prompt: str) -> str:
    clean = (prompt or "").strip()
    if not clean:
        raise ValueError("Rasm yaratish uchun prompt yuboring.")
    if len(clean) > MAX_PROMPT_LENGTH:
        raise ValueError(f"Prompt juda uzun. Maksimal {MAX_PROMPT_LENGTH} belgi.")
    return clean


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
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params) as response:
            if response.status >= 500:
                raise RuntimeError("Pollinations xizmati vaqtincha ishlamayapti.")
            if response.status >= 400:
                body = (await response.text())[:180]
                raise RuntimeError(
                    f"Pollinations API xatosi: HTTP {response.status}. {body}"
                )
            content = await response.read()

    if not content:
        raise RuntimeError("Pollinations bo'sh rasm qaytardi.")
    return content
