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


def grok_input_usd_per_million() -> float:
    return max(0.0, _read_float("AI_GROK_INPUT_USD_PER_MILLION", 0.20))


def grok_output_usd_per_million() -> float:
    return max(0.0, _read_float("AI_GROK_OUTPUT_USD_PER_MILLION", 0.50))


def grok_search_tool_call_usd() -> float:
    return max(0.0, _read_float("AI_GROK_SEARCH_TOOL_CALL_USD", 0.005))


def imagen_fast_usd_per_image() -> float:
    return max(0.0, _read_float("AI_IMAGEN_FAST_USD_PER_IMAGE", 0.02))


def premium_credit_value_usd() -> float:
    credits = max(1, premium_monthly_credits())
    return premium_safe_ai_budget_usd() / float(credits)


def estimate_grok_chat_cost_usd(*, prompt_tokens: int, completion_tokens: int) -> float:
    input_cost = (max(0, int(prompt_tokens)) / 1_000_000.0) * grok_input_usd_per_million()
    output_cost = (max(0, int(completion_tokens)) / 1_000_000.0) * grok_output_usd_per_million()
    return round(input_cost + output_cost, 6)


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
        "grok_search_tool_call_usd": grok_search_tool_call_usd(),
    }
