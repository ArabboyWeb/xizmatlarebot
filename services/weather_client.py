import html
from typing import Any

import aiohttp

from services.load_control import run_with_limit

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_REVERSE_URL = "https://geocoding-api.open-meteo.com/v1/reverse"
HTTP_TIMEOUT_SECONDS = 12

WEATHER_CODES: dict[int, str] = {
    0: "Ochiq osmon",
    1: "Asosan ochiq",
    2: "Qisman bulutli",
    3: "Bulutli",
    45: "Tuman",
    48: "Qalin tuman",
    51: "Mayin yomg'ir",
    53: "Yomg'ir",
    55: "Kuchli yomg'ir",
    56: "Muzli mayin yomg'ir",
    57: "Muzli yomg'ir",
    61: "Yengil yomg'ir",
    63: "O'rtacha yomg'ir",
    65: "Kuchli yomg'ir",
    66: "Muzli yomg'ir",
    67: "Kuchli muzli yomg'ir",
    71: "Yengil qor",
    73: "Qor",
    75: "Kuchli qor",
    77: "Qor donachalari",
    80: "Yengil jala",
    81: "Jala",
    82: "Kuchli jala",
    85: "Yengil qor jala",
    86: "Kuchli qor jala",
    95: "Momaqaldiroq",
    96: "Do'l bilan momaqaldiroq",
    99: "Kuchli do'l bilan momaqaldiroq",
}


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_number(value: float | None, suffix: str) -> str:
    if value is None:
        return "Noma'lum"
    return f"{value:.1f}{suffix}"


def _weather_description(code: Any) -> str:
    with_code = str(code) if code is not None else "?"
    try:
        parsed = int(code)
    except (TypeError, ValueError):
        return f"Noma'lum ob-havo kodi ({html.escape(with_code)})"
    default_text = "Noma'lum ob-havo holati"
    return f"{WEATHER_CODES.get(parsed, default_text)} ({parsed})"


async def _request_json(
    url: str, params: dict[str, str | float | int]
) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                if response.status >= 500:
                    raise RuntimeError(
                        "Servis vaqtincha ishlamayapti, keyinroq urinib ko'ring."
                    )
                response.raise_for_status()
                payload = await response.json()

        if not isinstance(payload, dict):
            raise RuntimeError("API noto'g'ri javob qaytardi.")
        return payload

    return await run_with_limit("api", _run)


async def resolve_city(city: str) -> dict[str, Any]:
    payload = await _request_json(
        OPEN_METEO_GEOCODE_URL,
        {"name": city, "count": 1, "language": "uz", "format": "json"},
    )
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("Shahar topilmadi. Nomni aniqroq yozib qayta urinib ko'ring.")

    item = results[0]
    if not isinstance(item, dict):
        raise RuntimeError("Geocoding javobi yaroqsiz.")

    lat = _to_float(item.get("latitude"))
    lon = _to_float(item.get("longitude"))
    if lat is None or lon is None:
        raise RuntimeError("Shahar koordinatalari topilmadi.")

    return {
        "latitude": lat,
        "longitude": lon,
        "name": str(item.get("name", city)),
        "country": str(item.get("country", "")),
    }


async def reverse_location_name(latitude: float, longitude: float) -> str:
    payload = await _request_json(
        OPEN_METEO_REVERSE_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "count": 1,
            "language": "uz",
            "format": "json",
        },
    )
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return f"{latitude:.4f}, {longitude:.4f}"

    item = results[0]
    if not isinstance(item, dict):
        return f"{latitude:.4f}, {longitude:.4f}"

    name = str(item.get("name", "")).strip()
    country = str(item.get("country", "")).strip()
    if name and country:
        return f"{name}, {country}"
    if name:
        return name
    return f"{latitude:.4f}, {longitude:.4f}"


async def fetch_current_weather(latitude: float, longitude: float) -> dict[str, Any]:
    payload = await _request_json(
        OPEN_METEO_FORECAST_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code",
            "timezone": "auto",
        },
    )
    current = payload.get("current")
    if not isinstance(current, dict):
        raise RuntimeError("Ob-havo ma'lumotini olishda xatolik yuz berdi.")
    return current


def build_weather_html(location_name: str, current: dict[str, Any]) -> str:
    temperature = _to_float(current.get("temperature_2m"))
    humidity = _to_float(current.get("relative_humidity_2m"))
    wind_speed = _to_float(current.get("wind_speed_10m"))
    weather_code = current.get("weather_code")

    safe_location = html.escape(location_name or "Noma'lum joy")
    safe_desc = html.escape(_weather_description(weather_code))

    return (
        "<b>Ob-havo ma'lumoti</b>\n"
        f"<b>Joy:</b> {safe_location}\n"
        f"<b>Holat:</b> {safe_desc}\n\n"
        f"Harorat: <b>{_format_number(temperature, ' C')}</b>\n"
        f"Namlik: <b>{_format_number(humidity, '%')}</b>\n"
        f"Shamol: <b>{_format_number(wind_speed, ' km/soat')}</b>"
    )
