from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from services.ai_costs import estimate_model_chat_cost_usd
from services.load_control import run_with_limit
from services.token_pricing import ai_min_cost, premium_ai_chat_credit_cost

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
DEFAULT_RESPONSE_STYLE_PROMPT = (
    "Javoblarni soddalashtirilgan Markdown uslubida yozing: qisqa sarlavha, punktlar, "
    "qisqa paragraf, inline code va kerak bo'lsa fenced code block ishlating. "
    "Jadvallarni ishlatmang, ortiqcha bezak bermang."
)
MODEL_ALIAS_AUTO = "auto"
PLAN_LEVELS = {"free": 0, "premium": 1}


@dataclass(slots=True)
class AIRouteDecision:
    provider: str
    model: str
    route: str
    credit_multiplier: int
    effective_plan: str
    model_alias: str
    max_output_tokens: int


@dataclass(slots=True)
class AIResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    provider: str
    model: str
    route: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class AIModelSpec:
    provider: str
    model: str
    credit_cost: int
    label: str
    max_output_tokens: int


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = _env(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _openrouter_headers() -> dict[str, str]:
    api_key = _env("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("AI provider kaliti topilmadi.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": _env("OPENROUTER_HTTP_REFERER", DEFAULT_REFERER),
        "X-Title": _env("OPENROUTER_X_TITLE", DEFAULT_TITLE),
    }


def _system_prompt() -> str:
    return _env("AI_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)


def _response_style_prompt() -> str:
    return _env("AI_RESPONSE_STYLE_PROMPT", DEFAULT_RESPONSE_STYLE_PROMPT)


def _free_simple_model() -> str:
    return _env("AI_FREE_MODEL_SIMPLE", "z-ai/glm-4.5-air:free")


def _free_complex_model() -> str:
    return _env("AI_FREE_MODEL_COMPLEX", "qwen/qwen3-vl-235b-a22b-thinking")


def _premium_chat_model() -> str:
    return _env(
        "AI_PREMIUM_CHAT_MODEL",
        _env("AI_PREMIUM_MODEL_COMPLEX", "x-ai/grok-4.1-fast"),
    )


def _premium_hunter_model() -> str:
    return _env("AI_PREMIUM_MODEL_HUNTER", "openrouter/hunter-alpha")


def _premium_deepseek_model() -> str:
    return _env("AI_PREMIUM_MODEL_DEEPSEEK", "deepseek/deepseek-v3.2")


def _premium_step_model() -> str:
    return _env("AI_PREMIUM_MODEL_STEP", "stepfun/step-3.5-flash:free")


def _premium_qwen_model() -> str:
    return _env("AI_PREMIUM_MODEL_QWEN", _free_complex_model())


def _premium_glm_model() -> str:
    return _env("AI_PREMIUM_MODEL_GLM", _free_simple_model())


def _free_max_output_tokens() -> int:
    return max(256, _env_int("AI_FREE_MAX_OUTPUT_TOKENS", 700))


def _premium_max_output_tokens() -> int:
    return max(256, _env_int("AI_PREMIUM_MAX_OUTPUT_TOKENS", 800))


def _premium_model_credit_cost(name: str, default: int) -> int:
    return max(1, _env_int(name, default))


def _premium_model_max_output_tokens(name: str, default: int) -> int:
    return max(256, _env_int(name, default))


def plan_level(plan: str) -> int:
    return PLAN_LEVELS.get((plan or "free").strip().lower(), 0)


def clamp_selected_plan(selected_plan: str, current_plan: str) -> str:
    normalized_selected = (selected_plan or MODEL_ALIAS_AUTO).strip().lower()
    if normalized_selected == MODEL_ALIAS_AUTO:
        return MODEL_ALIAS_AUTO
    if normalized_selected not in PLAN_LEVELS:
        return MODEL_ALIAS_AUTO
    current = (current_plan or "free").strip().lower()
    if plan_level(normalized_selected) > plan_level(current):
        return current
    return normalized_selected


def effective_selected_plan(user: dict[str, Any]) -> str:
    current_plan = str(user.get("current_plan", "free") or "free").strip().lower()
    selected_plan = clamp_selected_plan(
        str(user.get("selected_plan", MODEL_ALIAS_AUTO) or MODEL_ALIAS_AUTO),
        current_plan,
    )
    return current_plan if selected_plan == MODEL_ALIAS_AUTO else selected_plan


LEGACY_MODEL_ALIASES = {
    "grok_46": "premium_grok_fast",
    "premium_grok_46": "premium_grok_fast",
    "free_qwen": "premium_qwen",
}


def _manual_model_specs() -> dict[str, AIModelSpec]:
    premium_default_cost = premium_ai_chat_credit_cost()
    return {
        "free_glm": AIModelSpec(
            provider="openrouter",
            model=_free_simple_model(),
            credit_cost=0,
            label="GLM 4.5 Air",
            max_output_tokens=_free_max_output_tokens(),
        ),
        "premium_grok_fast": AIModelSpec(
            provider="openrouter",
            model=_premium_chat_model(),
            credit_cost=_premium_model_credit_cost(
                "AI_PREMIUM_CHAT_CREDITS_GROK",
                premium_default_cost,
            ),
            label="Grok 4.6",
            max_output_tokens=_premium_model_max_output_tokens(
                "AI_PREMIUM_MAX_OUTPUT_TOKENS_GROK",
                _premium_max_output_tokens(),
            ),
        ),
        "premium_hunter_alpha": AIModelSpec(
            provider="openrouter",
            model=_premium_hunter_model(),
            credit_cost=_premium_model_credit_cost(
                "AI_PREMIUM_CHAT_CREDITS_HUNTER",
                premium_default_cost,
            ),
            label="Hunter Alpha",
            max_output_tokens=_premium_model_max_output_tokens(
                "AI_PREMIUM_MAX_OUTPUT_TOKENS_HUNTER",
                _premium_max_output_tokens(),
            ),
        ),
        "premium_deepseek_v32": AIModelSpec(
            provider="openrouter",
            model=_premium_deepseek_model(),
            credit_cost=_premium_model_credit_cost(
                "AI_PREMIUM_CHAT_CREDITS_DEEPSEEK",
                premium_default_cost,
            ),
            label="DeepSeek V3.2",
            max_output_tokens=_premium_model_max_output_tokens(
                "AI_PREMIUM_MAX_OUTPUT_TOKENS_DEEPSEEK",
                _premium_max_output_tokens(),
            ),
        ),
        "premium_qwen": AIModelSpec(
            provider="openrouter",
            model=_premium_qwen_model(),
            credit_cost=_premium_model_credit_cost(
                "AI_PREMIUM_CHAT_CREDITS_QWEN",
                premium_default_cost,
            ),
            label="Qwen 3 Thinking",
            max_output_tokens=_premium_model_max_output_tokens(
                "AI_PREMIUM_MAX_OUTPUT_TOKENS_QWEN",
                max(256, _premium_max_output_tokens() - 100),
            ),
        ),
        "premium_step_35_flash": AIModelSpec(
            provider="openrouter",
            model=_premium_step_model(),
            credit_cost=_premium_model_credit_cost(
                "AI_PREMIUM_CHAT_CREDITS_STEP",
                premium_default_cost,
            ),
            label="Step 3.5 Flash",
            max_output_tokens=_premium_model_max_output_tokens(
                "AI_PREMIUM_MAX_OUTPUT_TOKENS_STEP",
                max(256, _premium_max_output_tokens() - 50),
            ),
        ),
        "premium_glm": AIModelSpec(
            provider="openrouter",
            model=_premium_glm_model(),
            credit_cost=_premium_model_credit_cost(
                "AI_PREMIUM_CHAT_CREDITS_GLM",
                premium_default_cost,
            ),
            label="GLM 4.5 Air",
            max_output_tokens=_premium_model_max_output_tokens(
                "AI_PREMIUM_MAX_OUTPUT_TOKENS_GLM",
                max(256, _premium_max_output_tokens() - 50),
            ),
        ),
    }


def allowed_model_aliases_for_plan(plan: str) -> list[str]:
    normalized = (plan or "free").strip().lower()
    if normalized == "premium":
        return [
            "premium_grok_fast",
            "premium_hunter_alpha",
            "premium_deepseek_v32",
            "premium_qwen",
            "premium_step_35_flash",
            "premium_glm",
        ]
    return ["free_glm"]


def normalize_selected_model_alias(selected_model_alias: str, effective_plan: str) -> str:
    normalized_alias = str(selected_model_alias or MODEL_ALIAS_AUTO).strip().lower()
    normalized_alias = LEGACY_MODEL_ALIASES.get(normalized_alias, normalized_alias)
    if normalized_alias == "free_glm" and (effective_plan or "").strip().lower() == "premium":
        normalized_alias = "premium_glm"
    if normalized_alias == MODEL_ALIAS_AUTO:
        return MODEL_ALIAS_AUTO
    if normalized_alias not in _manual_model_specs():
        return MODEL_ALIAS_AUTO
    if normalized_alias not in allowed_model_aliases_for_plan(effective_plan):
        return MODEL_ALIAS_AUTO
    return normalized_alias


def model_credit_cost(alias: str, *, plan: str) -> int:
    normalized_plan = (plan or "free").strip().lower()
    normalized_alias = normalize_selected_model_alias(alias, normalized_plan)
    if normalized_plan != "premium" or normalized_alias == MODEL_ALIAS_AUTO:
        return 0
    spec = _manual_model_specs().get(normalized_alias)
    return max(0, int(spec.credit_cost if spec is not None else premium_ai_chat_credit_cost()))


def premium_model_credit_range() -> tuple[int, int]:
    costs = [model_credit_cost(alias, plan="premium") for alias in allowed_model_aliases_for_plan("premium")]
    if not costs:
        base = premium_ai_chat_credit_cost()
        return base, base
    return min(costs), max(costs)


def model_options_for_plan(plan: str) -> list[tuple[str, str]]:
    specs = _manual_model_specs()
    options = [(MODEL_ALIAS_AUTO, "Auto")]
    for alias in allowed_model_aliases_for_plan(plan):
        spec = specs[alias]
        if (plan or "free").strip().lower() == "premium":
            options.append((alias, f"{spec.label} • {spec.credit_cost} kr"))
        else:
            options.append((alias, spec.label))
    return options


def model_label(alias: str) -> str:
    normalized = (alias or MODEL_ALIAS_AUTO).strip().lower()
    if normalized == MODEL_ALIAS_AUTO:
        return "Auto"
    normalized = LEGACY_MODEL_ALIASES.get(normalized, normalized)
    spec = _manual_model_specs().get(normalized)
    return spec.label if spec is not None else normalized


def _manual_route_decision(selected_model_alias: str, effective_plan: str) -> AIRouteDecision | None:
    normalized_alias = normalize_selected_model_alias(selected_model_alias, effective_plan)
    if normalized_alias == MODEL_ALIAS_AUTO:
        return None
    spec = _manual_model_specs()[normalized_alias]
    return AIRouteDecision(
        provider=spec.provider,
        model=spec.model,
        route=f"manual_{effective_plan}",
        credit_multiplier=spec.credit_cost,
        effective_plan=effective_plan,
        model_alias=normalized_alias,
        max_output_tokens=spec.max_output_tokens,
    )


def _parse_openrouter_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("AI javobi bo'sh qaytdi.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError("AI javobi noto'g'ri formatda keldi.")
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
    raise RuntimeError("AI text javobi topilmadi.")


def _parse_openai_text(payload: dict[str, Any]) -> str:
    direct_text = str(payload.get("output_text", "") or "").strip()
    if direct_text:
        return direct_text
    output = payload.get("output")
    if not isinstance(output, list):
        raise RuntimeError("AI javobi bo'sh qaytdi.")
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
    raise RuntimeError("AI text javobi topilmadi.")


def _parse_google_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError("AI javobi bo'sh qaytdi.")
    content = candidates[0].get("content")
    if not isinstance(content, dict):
        raise RuntimeError("AI javobi noto'g'ri formatda keldi.")
    parts = content.get("parts")
    if not isinstance(parts, list):
        raise RuntimeError("AI javobi noto'g'ri formatda keldi.")
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = str(part.get("text", "") or "").strip()
        if text:
            chunks.append(text)
    if chunks:
        return "\n".join(chunks).strip()
    raise RuntimeError("AI text javobi topilmadi.")


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


def _approx_token_count(text: str) -> int:
    clean = str(text or "").strip()
    return max(1, math.ceil(len(clean) / 4))


def _api_error_text(payload: Any, status: int) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message", "") or "").strip()
            if message:
                return f"{status}: {message}"
        message = str(payload.get("message", "") or "").strip()
        if message:
            return f"{status}: {message}"
    return f"{status}: API request failed"


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


def select_route(
    user_text: str,
    *,
    current_plan: str,
    effective_plan: str,
    selected_model_alias: str = MODEL_ALIAS_AUTO,
) -> AIRouteDecision:
    manual_decision = _manual_route_decision(selected_model_alias, effective_plan)
    if manual_decision is not None:
        return manual_decision

    complexity = _complexity(user_text)
    if effective_plan == "free":
        spec = _manual_model_specs()["free_glm"]
        return AIRouteDecision(
            provider=spec.provider,
            model=spec.model,
            route=f"free_{complexity}",
            credit_multiplier=0,
            effective_plan="free",
            model_alias="free_glm",
            max_output_tokens=spec.max_output_tokens,
        )

    if effective_plan == "premium":
        spec = _manual_model_specs()["premium_grok_fast"]
        return AIRouteDecision(
            provider=spec.provider,
            model=spec.model,
            route=f"premium_{complexity}",
            credit_multiplier=spec.credit_cost,
            effective_plan="premium",
            model_alias="premium_grok_fast",
            max_output_tokens=spec.max_output_tokens,
        )

    spec = _manual_model_specs()["premium_grok_fast"]
    return AIRouteDecision(
        provider=spec.provider,
        model=spec.model,
        route="premium_complex",
        credit_multiplier=spec.credit_cost,
        effective_plan="premium",
        model_alias="premium_grok_fast",
        max_output_tokens=spec.max_output_tokens,
    )


def estimate_credits(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    decision: AIRouteDecision,
) -> int:
    if decision.effective_plan == "free":
        return 0
    _ = (prompt_tokens, completion_tokens)
    return max(ai_min_cost(decision.effective_plan), int(decision.credit_multiplier))


def projected_credits(
    *,
    user_text: str,
    current_plan: str,
    effective_plan: str,
    selected_model_alias: str = MODEL_ALIAS_AUTO,
) -> int:
    decision = select_route(
        user_text,
        current_plan=current_plan,
        effective_plan=effective_plan,
        selected_model_alias=selected_model_alias,
    )
    if decision.effective_plan == "free":
        return 0
    return max(ai_min_cost(decision.effective_plan), int(decision.credit_multiplier))


def projected_ai_cost_usd(
    *,
    user_text: str,
    history: list[dict[str, str]],
    current_plan: str,
    effective_plan: str,
    selected_model_alias: str = MODEL_ALIAS_AUTO,
) -> float:
    decision = select_route(
        user_text,
        current_plan=current_plan,
        effective_plan=effective_plan,
        selected_model_alias=selected_model_alias,
    )
    if decision.effective_plan != "premium":
        return 0.0
    messages = build_messages(history, user_text)
    prompt_tokens = _approx_token_count(_conversation_text(messages))
    return estimate_model_chat_cost_usd(
        model_alias=decision.model_alias,
        prompt_tokens=prompt_tokens,
        completion_tokens=decision.max_output_tokens,
    )


def build_messages(history: list[dict[str, str]], user_text: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": f"{_system_prompt()}\n\n{_response_style_prompt()}".strip(),
        }
    ]
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


async def _openrouter_completion(
    messages: list[dict[str, str]],
    model: str,
    route: str,
    *,
    max_output_tokens: int,
) -> AIResult:
    started = time.perf_counter()
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": max(1, int(max_output_tokens)),
    }
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, headers=_openrouter_headers()) as session:
        async with session.post(OPENROUTER_URL, json=payload) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(_api_error_text(data, response.status))
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


def _stream_delta_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "") or "").strip()
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    return ""


async def _openrouter_completion_stream(
    messages: list[dict[str, str]],
    model: str,
    route: str,
    *,
    max_output_tokens: int,
    on_text: Callable[[str], Awaitable[None]] | None,
) -> AIResult:
    started = time.perf_counter()
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "stream": True,
        "max_tokens": max(1, int(max_output_tokens)),
    }
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    text_chunks: list[str] = []
    usage: dict[str, Any] = {}
    buffer = ""
    async with aiohttp.ClientSession(timeout=timeout, headers=_openrouter_headers()) as session:
        async with session.post(OPENROUTER_URL, json=payload) as response:
            if response.status >= 400:
                data = await response.json(content_type=None)
                raise RuntimeError(_api_error_text(data, response.status))
            async for raw_chunk in response.content:
                if not raw_chunk:
                    continue
                buffer += raw_chunk.decode("utf-8", errors="ignore")
                while "\n\n" in buffer:
                    event, buffer = buffer.split("\n\n", 1)
                    data_lines: list[str] = []
                    for raw_line in event.splitlines():
                        line = raw_line.strip()
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                    if not data_lines:
                        continue
                    payload_text = "\n".join(data_lines).strip()
                    if not payload_text or payload_text == "[DONE]":
                        continue
                    try:
                        stream_payload = json.loads(payload_text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(stream_payload, dict) and isinstance(
                        stream_payload.get("error"), dict
                    ):
                        raise RuntimeError(
                            _api_error_text(
                                {"error": stream_payload.get("error")},
                                200,
                            )
                        )
                    if isinstance(stream_payload, dict) and isinstance(
                        stream_payload.get("usage"), dict
                    ):
                        usage = stream_payload.get("usage") or {}
                    delta_text = (
                        _stream_delta_text(stream_payload)
                        if isinstance(stream_payload, dict)
                        else ""
                    )
                    if not delta_text:
                        continue
                    text_chunks.append(delta_text)
                    if on_text is not None:
                        await on_text("".join(text_chunks))
    final_text = "".join(text_chunks).strip()
    if not final_text:
        raise RuntimeError("AI text javobi topilmadi.")
    prompt_tokens = _usage_value({"u": usage}, "u", "prompt_tokens")
    completion_tokens = _usage_value({"u": usage}, "u", "completion_tokens")
    return AIResult(
        text=final_text,
        prompt_tokens=prompt_tokens or _approx_token_count(_conversation_text(messages)),
        completion_tokens=completion_tokens or _approx_token_count(final_text),
        provider="openrouter",
        model=model,
        route=route,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


async def _openai_completion(prompt_text: str, model: str, route: str) -> AIResult:
    api_key = _env("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("401: Official provider kaliti topilmadi")
    started = time.perf_counter()
    payload = {"model": model, "input": prompt_text}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(OPENAI_RESPONSES_URL, json=payload) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                raise RuntimeError(_api_error_text(data, response.status))
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
    api_key = _env("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("401: Official provider kaliti topilmadi")
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
                raise RuntimeError(_api_error_text(data, response.status))
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
    selected_model_alias: str = MODEL_ALIAS_AUTO,
    on_text: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[AIRouteDecision, AIResult]:
    async def _run() -> tuple[AIRouteDecision, AIResult]:
        decision = select_route(
            user_text,
            current_plan=current_plan,
            effective_plan=effective_plan,
            selected_model_alias=selected_model_alias,
        )
        messages = build_messages(history, user_text)
        if decision.provider == "openrouter":
            if on_text is not None:
                result = await _openrouter_completion_stream(
                    messages,
                    decision.model,
                    decision.route,
                    max_output_tokens=decision.max_output_tokens,
                    on_text=on_text,
                )
            else:
                result = await _openrouter_completion(
                    messages,
                    decision.model,
                    decision.route,
                    max_output_tokens=decision.max_output_tokens,
                )
        elif decision.provider == "google":
            result = await _google_completion(
                _conversation_text(messages),
                decision.model,
                decision.route,
            )
        else:
            result = await _openai_completion(
                _conversation_text(messages),
                decision.model,
                decision.route,
            )
        if on_text is not None and decision.provider != "openrouter":
            await on_text(result.text)
        return decision, result

    return await run_with_limit("ai", _run)
