from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
GOOGLE_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
HTTP_TIMEOUT_SECONDS = 120
DEFAULT_REFERER = "https://github.com/ArabboyWeb/xizmatlarebot"
DEFAULT_TITLE = "Xizmatlar E-Bot AI"
DEFAULT_SYSTEM_PROMPT = (
    "Siz Xizmatlar E-Bot ichidagi sun'iy intellekt yordamchisiz. "
    "Aniq, foydali va qisqa javob bering. Agar foydalanuvchi boshqa til so'ramasa, "
    "o'zbek tilida javob qaytaring."
)


@dataclass(slots=True)
class AIRouteDecision:
    provider: str
    model: str
    route: str
    credit_multiplier: int
    effective_plan: str


@dataclass(slots=True)
class AIResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    provider: str
    model: str
    route: str
    latency_ms: int


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _openrouter_headers() -> dict[str, str]:
    api_key = _env("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY topilmadi.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": _env("OPENROUTER_HTTP_REFERER", DEFAULT_REFERER),
        "X-Title": _env("OPENROUTER_X_TITLE", DEFAULT_TITLE),
    }


def _system_prompt() -> str:
    return _env("AI_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)


def _parse_openrouter_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("OpenRouter javobi bo'sh qaytdi.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError("OpenRouter message topilmadi.")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "") or "").strip()
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts).strip()
    raise RuntimeError("OpenRouter text javobi topilmadi.")


def _parse_openai_text(payload: dict[str, Any]) -> str:
    direct_text = str(payload.get("output_text", "") or "").strip()
    if direct_text:
        return direct_text
    output = payload.get("output")
    if not isinstance(output, list):
        raise RuntimeError("OpenAI javobi bo'sh qaytdi.")
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = str(part.get("text", "") or "").strip()
            if text:
                chunks.append(text)
    if chunks:
        return "\n".join(chunks).strip()
    raise RuntimeError("OpenAI text javobi topilmadi.")


def _parse_google_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError("Google Gemini javobi bo'sh qaytdi.")
    content = candidates[0].get("content")
    if not isinstance(content, dict):
        raise RuntimeError("Google Gemini content topilmadi.")
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise RuntimeError("Google Gemini parts topilmadi.")
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = str(part.get("text", "") or "").strip()
        if text:
            chunks.append(text)
    if chunks:
        return "\n".join(chunks).strip()
    raise RuntimeError("Google Gemini text javobi topilmadi.")


def _usage_value(payload: dict[str, Any], *keys: str) -> int:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return 0
        current = current.get(key)
    try:
        return int(current or 0)
    except (TypeError, ValueError):
        return 0


def _complexity(text: str) -> str:
    clean = (text or "").strip()
    lowered = clean.lower()
    lines = clean.count("\n") + 1
    length = len(clean)
    keywords = (
        "code",
        "python",
        "debug",
        "analysis",
        "architect",
        "compare",
        "explain",
        "optimize",
        "step by step",
        "mathematics",
    )
    score = 0
    if length > 160:
        score += 1
    if length > 500:
        score += 2
    if lines > 6:
        score += 1
    if "```" in clean:
        score += 2
    if any(word in lowered for word in keywords):
        score += 2
    if length < 40 and lines == 1:
        score -= 1
    if score <= 0:
        return "simple"
    if score <= 2:
        return "standard"
    return "complex"


def select_route(user_text: str, *, current_plan: str, effective_plan: str) -> AIRouteDecision:
    complexity = _complexity(user_text)
    if effective_plan == "free":
        model = _env("AI_FREE_MODEL_SIMPLE", "z-ai/glm-4.5-air:free")
        if complexity in {"standard", "complex"}:
            model = _env(
                "AI_FREE_MODEL_COMPLEX",
                "qwen/qwen3-vl-235b-a22b-thinking",
            )
        return AIRouteDecision(
            provider="openrouter",
            model=model,
            route=f"free_{complexity}",
            credit_multiplier=1,
            effective_plan="free",
        )

    if effective_plan == "premium":
        if complexity == "simple":
            return AIRouteDecision(
                provider="openrouter",
                model=_env("AI_PREMIUM_MODEL_SIMPLE", "openai/gpt-5-mini"),
                route="premium_simple",
                credit_multiplier=1,
                effective_plan="premium",
            )
        return AIRouteDecision(
            provider="openrouter",
            model=_env("AI_PREMIUM_MODEL_COMPLEX", "x-ai/grok-4.1-fast"),
            route="premium_complex",
            credit_multiplier=2,
            effective_plan="premium",
        )

    if complexity == "simple":
        return AIRouteDecision(
            provider="openrouter",
            model=_env("AI_PREMIUM_MODEL_SIMPLE", "openai/gpt-5-mini"),
            route="pro_simple_to_premium",
            credit_multiplier=1,
            effective_plan="pro",
        )
    if complexity == "standard":
        return AIRouteDecision(
            provider="openrouter",
            model=_env("AI_PREMIUM_MODEL_COMPLEX", "x-ai/grok-4.1-fast"),
            route="pro_standard_to_premium",
            credit_multiplier=2,
            effective_plan="pro",
        )

    provider = _env("AI_PRO_PROVIDER", "openai").lower()
    if provider == "google":
        model = _env("AI_PRO_GOOGLE_MODEL", "gemini-3.1-pro-preview")
    else:
        provider = "openai"
        model = _env("AI_PRO_OPENAI_MODEL", "gpt-5.2")
    return AIRouteDecision(
        provider=provider,
        model=model,
        route=f"pro_official_{provider}",
        credit_multiplier=3,
        effective_plan="pro",
    )


def estimate_credits(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    decision: AIRouteDecision,
) -> int:
    if decision.effective_plan == "free":
        return 1
    token_unit = max(100, int(_env("AI_CREDIT_TOKEN_UNIT", "1000")))
    total_tokens = max(1, int(prompt_tokens) + int(completion_tokens))
    base_units = max(1, math.ceil(total_tokens / token_unit))
    return base_units * max(1, int(decision.credit_multiplier))


def build_messages(history: list[dict[str, str]], user_text: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": _system_prompt()}]
    for item in history:
        role = str(item.get("role", "user") or "user").strip().lower()
        content = str(item.get("content", "") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": str(user_text or "").strip()})
    return messages


def _conversation_text(messages: list[dict[str, str]]) -> str:
    rows: list[str] = []
    for item in messages:
        role = str(item.get("role", "user") or "user").strip().lower()
        content = str(item.get("content", "") or "").strip()
        if not content:
            continue
        label = "System" if role == "system" else ("Assistant" if role == "assistant" else "User")
        rows.append(f"{label}: {content}")
    rows.append("Assistant:")
    return "\n\n".join(rows)


async def _openrouter_completion(messages: list[dict[str, str]], model: str, route: str) -> AIResult:
    started = time.perf_counter()
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
    }
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, headers=_openrouter_headers()) as session:
        async with session.post(OPENROUTER_URL, json=payload) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(str(data)[:200])
    usage = data.get("usage") if isinstance(data, dict) else {}
    return AIResult(
        text=_parse_openrouter_text(data),
        prompt_tokens=_usage_value({"u": usage}, "u", "prompt_tokens"),
        completion_tokens=_usage_value({"u": usage}, "u", "completion_tokens"),
        provider="openrouter",
        model=model,
        route=route,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


async def _openai_completion(prompt_text: str, model: str, route: str) -> AIResult:
    api_key = _env("AI_PRO_OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("AI_PRO_OPENAI_API_KEY topilmadi.")
    started = time.perf_counter()
    payload = {"model": model, "input": prompt_text}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(OPENAI_RESPONSES_URL, json=payload) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(str(data)[:200])
    usage = data.get("usage") if isinstance(data, dict) else {}
    return AIResult(
        text=_parse_openai_text(data),
        prompt_tokens=_usage_value({"u": usage}, "u", "input_tokens"),
        completion_tokens=_usage_value({"u": usage}, "u", "output_tokens"),
        provider="openai",
        model=model,
        route=route,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


async def _google_completion(prompt_text: str, model: str, route: str) -> AIResult:
    api_key = _env("AI_PRO_GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("AI_PRO_GOOGLE_API_KEY topilmadi.")
    started = time.perf_counter()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.4},
    }
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    url = GOOGLE_API_URL.format(model=model)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, params={"key": api_key}, json=payload) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(str(data)[:200])
    usage = data.get("usageMetadata") if isinstance(data, dict) else {}
    return AIResult(
        text=_parse_google_text(data),
        prompt_tokens=_usage_value({"u": usage}, "u", "promptTokenCount"),
        completion_tokens=_usage_value({"u": usage}, "u", "candidatesTokenCount"),
        provider="google",
        model=model,
        route=route,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


async def generate_ai_reply(
    *,
    user_text: str,
    history: list[dict[str, str]],
    current_plan: str,
    effective_plan: str,
) -> tuple[AIRouteDecision, AIResult]:
    decision = select_route(
        user_text,
        current_plan=current_plan,
        effective_plan=effective_plan,
    )
    messages = build_messages(history, user_text)
    if decision.provider == "openrouter":
        result = await _openrouter_completion(messages, decision.model, decision.route)
    elif decision.provider == "google":
        result = await _google_completion(_conversation_text(messages), decision.model, decision.route)
    else:
        result = await _openai_completion(_conversation_text(messages), decision.model, decision.route)
    return decision, result
