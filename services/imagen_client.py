from __future__ import annotations

import base64
import os
from typing import Any

import aiohttp

HTTP_TIMEOUT_SECONDS = 120


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _access_token() -> str:
    token = (
        _env("GOOGLE_VERTEX_ACCESS_TOKEN")
        or _env("GOOGLE_CLOUD_ACCESS_TOKEN")
        or _env("VERTEX_AI_ACCESS_TOKEN")
    )
    if not token:
        raise RuntimeError("Imagen 4 Fast uchun Google Vertex access token topilmadi.")
    return token


def _project_id() -> str:
    project = _env("GOOGLE_VERTEX_PROJECT") or _env("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("GOOGLE_VERTEX_PROJECT sozlanmagan.")
    return project


def _location() -> str:
    return _env("GOOGLE_VERTEX_LOCATION", "us-central1")


def _model() -> str:
    return _env("AI_PREMIUM_IMAGE_MODEL", "imagen-4.0-fast-generate-001")


def _aspect_ratio(width: int, height: int) -> str:
    if width == height:
        return "1:1"
    if width > height:
        return "4:3"
    return "3:4"


def _endpoint() -> str:
    location = _location()
    project = _project_id()
    model = _model()
    return (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{location}/publishers/google/models/{model}:predict"
    )


def _prediction_bytes(prediction: Any) -> bytes:
    if not isinstance(prediction, dict):
        raise RuntimeError("Imagen javobi noto'g'ri formatda keldi.")
    for key in ("bytesBase64Encoded", "imageBytes"):
        raw = str(prediction.get(key, "") or "").strip()
        if raw:
            return base64.b64decode(raw)
    image = prediction.get("image")
    if isinstance(image, dict):
        for key in ("bytesBase64Encoded", "imageBytes"):
            raw = str(image.get(key, "") or "").strip()
            if raw:
                return base64.b64decode(raw)
    raise RuntimeError("Imagen javobida rasm topilmadi.")


async def generate_imagen_image(
    prompt: str,
    *,
    width: int,
    height: int,
) -> bytes:
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise ValueError("Prompt bo'sh bo'lishi mumkin emas.")

    payload = {
        "instances": [{"prompt": clean_prompt}],
        "parameters": {
            "sampleCount": 1,
            "aspectRatio": _aspect_ratio(width, height),
            "addWatermark": False,
            "safetyFilterLevel": "block_only_high",
            "personGeneration": "allow_adult",
        },
    }
    headers = {
        "Authorization": f"Bearer {_access_token()}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(_endpoint(), json=payload) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                message = ""
                if isinstance(data, dict):
                    error = data.get("error")
                    if isinstance(error, dict):
                        message = str(error.get("message", "") or "").strip()
                raise RuntimeError(message or f"Imagen API xatosi: {response.status}")
    predictions = data.get("predictions") if isinstance(data, dict) else None
    if not isinstance(predictions, list) or not predictions:
        raise RuntimeError("Imagen javobi bo'sh qaytdi.")
    return _prediction_bytes(predictions[0])
