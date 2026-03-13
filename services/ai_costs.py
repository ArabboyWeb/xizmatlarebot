from __future__ import annotations

import os

from services.token_pricing import (
    premium_monthly_credits,
    premium_price_uzs,
    premium_safe_ai_budget_usd,
)


def _read_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def premium_revenue_usd_estimate() -> float:
    return max(0.01, _read_float("AI_PREMIUM_REVENUE_USD_ESTIMATE", 1.64))


def _estimate_token_cost_usd(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    input_usd_per_million: float,
    output_usd_per_million: float,
) -> float:
    input_cost = (max(0, int(prompt_tokens)) / 1_000_000.0) * max(0.0, input_usd_per_million)
    output_cost = (max(0, int(completion_tokens)) / 1_000_000.0) * max(0.0, output_usd_per_million)
    return round(input_cost + output_cost, 6)


def grok_input_usd_per_million() -> float:
    return max(0.0, _read_float("AI_GROK_INPUT_USD_PER_MILLION", 0.20))


def grok_output_usd_per_million() -> float:
    return max(0.0, _read_float("AI_GROK_OUTPUT_USD_PER_MILLION", 0.50))


def deepseek_input_usd_per_million() -> float:
    return max(0.0, _read_float("AI_DEEPSEEK_INPUT_USD_PER_MILLION", 0.25))


def deepseek_output_usd_per_million() -> float:
    return max(0.0, _read_float("AI_DEEPSEEK_OUTPUT_USD_PER_MILLION", 0.40))


def qwen_input_usd_per_million() -> float:
    return max(0.0, _read_float("AI_QWEN_INPUT_USD_PER_MILLION", 0.0))


def qwen_output_usd_per_million() -> float:
    return max(0.0, _read_float("AI_QWEN_OUTPUT_USD_PER_MILLION", 0.0))


def hunter_input_usd_per_million() -> float:
    return max(0.0, _read_float("AI_HUNTER_INPUT_USD_PER_MILLION", 0.0))


def hunter_output_usd_per_million() -> float:
    return max(0.0, _read_float("AI_HUNTER_OUTPUT_USD_PER_MILLION", 0.0))


def step_input_usd_per_million() -> float:
    return max(0.0, _read_float("AI_STEP_INPUT_USD_PER_MILLION", 0.0))


def step_output_usd_per_million() -> float:
    return max(0.0, _read_float("AI_STEP_OUTPUT_USD_PER_MILLION", 0.0))


def glm_input_usd_per_million() -> float:
    return max(0.0, _read_float("AI_GLM_INPUT_USD_PER_MILLION", 0.0))


def glm_output_usd_per_million() -> float:
    return max(0.0, _read_float("AI_GLM_OUTPUT_USD_PER_MILLION", 0.0))


def grok_search_tool_call_usd() -> float:
    return max(0.0, _read_float("AI_GROK_SEARCH_TOOL_CALL_USD", 0.005))


def imagen_fast_usd_per_image() -> float:
    return max(0.0, _read_float("AI_IMAGEN_FAST_USD_PER_IMAGE", 0.02))


def premium_credit_value_usd() -> float:
    credits = max(1, premium_monthly_credits())
    return premium_safe_ai_budget_usd() / float(credits)


def estimate_grok_chat_cost_usd(*, prompt_tokens: int, completion_tokens: int) -> float:
    return _estimate_token_cost_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        input_usd_per_million=grok_input_usd_per_million(),
        output_usd_per_million=grok_output_usd_per_million(),
    )


def estimate_deepseek_chat_cost_usd(*, prompt_tokens: int, completion_tokens: int) -> float:
    return _estimate_token_cost_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        input_usd_per_million=deepseek_input_usd_per_million(),
        output_usd_per_million=deepseek_output_usd_per_million(),
    )


def estimate_qwen_chat_cost_usd(*, prompt_tokens: int, completion_tokens: int) -> float:
    return _estimate_token_cost_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        input_usd_per_million=qwen_input_usd_per_million(),
        output_usd_per_million=qwen_output_usd_per_million(),
    )


def estimate_hunter_chat_cost_usd(*, prompt_tokens: int, completion_tokens: int) -> float:
    return _estimate_token_cost_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        input_usd_per_million=hunter_input_usd_per_million(),
        output_usd_per_million=hunter_output_usd_per_million(),
    )


def estimate_step_chat_cost_usd(*, prompt_tokens: int, completion_tokens: int) -> float:
    return _estimate_token_cost_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        input_usd_per_million=step_input_usd_per_million(),
        output_usd_per_million=step_output_usd_per_million(),
    )


def estimate_glm_chat_cost_usd(*, prompt_tokens: int, completion_tokens: int) -> float:
    return _estimate_token_cost_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        input_usd_per_million=glm_input_usd_per_million(),
        output_usd_per_million=glm_output_usd_per_million(),
    )


def estimate_model_chat_cost_usd(
    *,
    model_alias: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    normalized = str(model_alias or "").strip().lower()
    if normalized == "premium_grok_fast":
        return estimate_grok_chat_cost_usd(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    if normalized == "premium_deepseek_v32":
        return estimate_deepseek_chat_cost_usd(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    if normalized in {"premium_qwen", "free_qwen"}:
        return estimate_qwen_chat_cost_usd(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    if normalized == "premium_hunter_alpha":
        return estimate_hunter_chat_cost_usd(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    if normalized == "premium_step_35_flash":
        return estimate_step_chat_cost_usd(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    if normalized in {"premium_glm", "free_glm"}:
        return estimate_glm_chat_cost_usd(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
    return estimate_grok_chat_cost_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def estimate_imagen_cost_usd(*, image_count: int = 1) -> float:
    return round(max(0, int(image_count)) * imagen_fast_usd_per_image(), 6)


def estimate_search_cost_usd(*, tool_calls: int = 6) -> float:
    return round(max(1, int(tool_calls)) * grok_search_tool_call_usd(), 6)


def premium_financial_snapshot() -> dict[str, float | int]:
    return {
        "premium_price_uzs": premium_price_uzs(),
        "premium_revenue_usd_estimate": premium_revenue_usd_estimate(),
        "premium_safe_ai_budget_usd": premium_safe_ai_budget_usd(),
        "premium_monthly_credits": premium_monthly_credits(),
        "credit_value_usd": round(premium_credit_value_usd(), 6),
        "imagen_fast_usd_per_image": imagen_fast_usd_per_image(),
        "grok_input_usd_per_million": grok_input_usd_per_million(),
        "grok_output_usd_per_million": grok_output_usd_per_million(),
        "deepseek_input_usd_per_million": deepseek_input_usd_per_million(),
        "deepseek_output_usd_per_million": deepseek_output_usd_per_million(),
        "qwen_input_usd_per_million": qwen_input_usd_per_million(),
        "qwen_output_usd_per_million": qwen_output_usd_per_million(),
        "hunter_input_usd_per_million": hunter_input_usd_per_million(),
        "hunter_output_usd_per_million": hunter_output_usd_per_million(),
        "step_input_usd_per_million": step_input_usd_per_million(),
        "step_output_usd_per_million": step_output_usd_per_million(),
        "glm_input_usd_per_million": glm_input_usd_per_million(),
        "glm_output_usd_per_million": glm_output_usd_per_million(),
        "grok_search_tool_call_usd": grok_search_tool_call_usd(),
    }
