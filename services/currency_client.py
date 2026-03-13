import html
from typing import Any

import aiohttp

from services.load_control import run_with_limit

CBU_CURRENCY_URL = "https://cbu.uz/uz/arkhiv-kursov-valyut/json/"
TRACKED_CODES = ("USD", "EUR", "RUB")
HTTP_TIMEOUT_SECONDS = 10


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _format_rate(value: float | None) -> str:
    if value is None:
        return "Mavjud emas"
    return f"{value:,.2f}".replace(",", " ")


async def fetch_currency_rates() -> tuple[dict[str, float | None], str]:
    async def _run() -> tuple[dict[str, float | None], str]:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(CBU_CURRENCY_URL) as response:
                response.raise_for_status()
                payload = await response.json()

        if not isinstance(payload, list) or not payload:
            raise RuntimeError("CBU API bo'sh yoki noto'g'ri javob qaytardi.")

        rates: dict[str, float | None] = {code: None for code in TRACKED_CODES}
        date_value = ""

        for item in payload:
            if not isinstance(item, dict):
                continue
            code = str(item.get("Ccy", "")).upper()
            if code in rates:
                rates[code] = _to_float(item.get("Rate"))
                if not date_value:
                    date_value = str(item.get("Date", "")).strip()

        return rates, date_value

    return await run_with_limit("api", _run)


def build_currency_text(rates: dict[str, float | None], date_value: str) -> str:
    safe_date = html.escape(date_value or "Noma'lum")
    usd = html.escape(_format_rate(rates.get("USD")))
    eur = html.escape(_format_rate(rates.get("EUR")))
    rub = html.escape(_format_rate(rates.get("RUB")))

    return (
        "<b>Valyuta kurslari</b>\n"
        "Manba: <b>CBU</b>\n"
        f"Sana: <code>{safe_date}</code>\n\n"
        f"USD: <b>{usd}</b> UZS\n"
        f"EUR: <b>{eur}</b> UZS\n"
        f"RUB: <b>{rub}</b> UZS\n\n"
        "Yangilash uchun pastdagi tugmani bosing."
    )
