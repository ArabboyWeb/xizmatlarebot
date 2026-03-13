import asyncio
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp

from services.load_control import run_with_limit
from services.rapidapi_client import HTTP_TIMEOUT_SECONDS, rapidapi_get

JSEARCH_HOST = "jsearch.p.rapidapi.com"
JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
STOP_WORDS = {
    "a",
    "an",
    "at",
    "for",
    "in",
    "job",
    "jobs",
    "of",
    "on",
    "role",
    "roles",
    "the",
    "vacancy",
    "vacancies",
    "with",
}


def _extract_jobs(payload: dict[str, Any]) -> list[dict[str, str]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []

    jobs: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("job_title", "")).strip()
        if not title:
            continue
        jobs.append(
            {
                "title": title,
                "company": str(item.get("employer_name", "")).strip(),
                "location": " ".join(
                    part
                    for part in [
                        str(item.get("job_city", "")).strip(),
                        str(item.get("job_state", "")).strip(),
                        str(item.get("job_country", "")).strip(),
                    ]
                    if part
                ).strip(),
                "type": str(item.get("job_employment_type", "")).strip(),
                "apply_link": str(item.get("job_apply_link", "")).strip()
                or str(item.get("job_google_link", "")).strip(),
                "posted": str(item.get("job_posted_at_datetime_utc", "")).strip(),
            }
        )
    return jobs


def _format_timestamp(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _split_query(query: str) -> tuple[str, str]:
    lowered = query.lower()
    marker = " in "
    if marker not in lowered:
        return query.strip(), ""
    left, right = query.rsplit(marker, maxsplit=1)
    keywords = left.strip()
    location = right.strip()
    if not keywords or not location:
        return query.strip(), ""
    return keywords, location


def _tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token and token not in STOP_WORDS
    ]


def _match_score(text: str, terms: list[str]) -> int:
    if not terms:
        return 0
    lowered = text.lower()
    return sum(1 for term in terms if term in lowered)


def _country_terms(country: str) -> list[str]:
    normalized = (country or "").strip().lower()
    if normalized == "us":
        return ["united states", "usa", "u.s.", "america"]
    if normalized == "uk":
        return ["united kingdom", "uk", "britain", "england"]
    return [normalized] if normalized else []


async def _get_json(url: str, params: dict[str, str | int] | None = None) -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (XizmatlarBot/1.0)",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, params=params) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}")
        if not isinstance(payload, dict):
            raise RuntimeError("Fallback jobs API noto'g'ri formatda javob qaytardi.")
        return payload

    return await run_with_limit("api", _run)


def _extract_remotive_jobs(
    payload: dict[str, Any],
    keyword_terms: list[str],
    location_terms: list[str],
    country_terms: list[str],
) -> list[tuple[int, dict[str, str]]]:
    rows = payload.get("jobs")
    if not isinstance(rows, list):
        return []

    jobs: list[tuple[int, dict[str, str]]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        company = str(item.get("company_name", "")).strip()
        location = str(item.get("candidate_required_location", "")).strip()
        tags = item.get("tags")
        tag_text = ", ".join(tag for tag in tags if isinstance(tag, str)) if isinstance(tags, list) else ""
        haystack = " ".join(
            value
            for value in [
                title,
                company,
                location,
                tag_text,
                str(item.get("category", "")).strip(),
                str(item.get("description", ""))[:700],
            ]
            if value
        )
        keyword_score = _match_score(haystack, keyword_terms)
        if keyword_terms and keyword_score == 0:
            continue
        location_score = _match_score(location, location_terms)
        country_score = _match_score(location, country_terms)
        score = keyword_score * 4 + location_score * 2 + country_score
        jobs.append(
            (
                score,
                {
                    "title": title,
                    "company": company,
                    "location": location,
                    "type": str(item.get("job_type", "")).strip(),
                    "apply_link": str(item.get("url", "")).strip(),
                    "posted": str(item.get("publication_date", "")).strip(),
                },
            )
        )
    return jobs


def _extract_arbeitnow_jobs(
    payload: dict[str, Any],
    keyword_terms: list[str],
    location_terms: list[str],
    country_terms: list[str],
) -> list[tuple[int, dict[str, str]]]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []

    jobs: list[tuple[int, dict[str, str]]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        company = str(item.get("company_name", "")).strip()
        location = str(item.get("location", "")).strip()
        tags = item.get("tags")
        tag_text = ", ".join(tag for tag in tags if isinstance(tag, str)) if isinstance(tags, list) else ""
        haystack = " ".join(
            value
            for value in [
                title,
                company,
                location,
                tag_text,
                str(item.get("description", ""))[:700],
            ]
            if value
        )
        keyword_score = _match_score(haystack, keyword_terms)
        if keyword_terms and keyword_score == 0:
            continue
        location_score = _match_score(location, location_terms)
        remote = bool(item.get("remote"))
        country_score = _match_score(location, country_terms)
        score = keyword_score * 4 + location_score * 3 + country_score + (
            1 if remote else 0
        )
        job_types = item.get("job_types")
        job_type = ", ".join(part for part in job_types if isinstance(part, str)) if isinstance(job_types, list) else ""
        if remote and job_type:
            job_type = f"{job_type}, remote"
        elif remote:
            job_type = "remote"
        jobs.append(
            (
                score,
                {
                    "title": title,
                    "company": company,
                    "location": location,
                    "type": job_type,
                    "apply_link": str(item.get("url", "")).strip(),
                    "posted": _format_timestamp(item.get("created_at")),
                },
            )
        )
    return jobs


def _merge_jobs(candidates: list[tuple[int, dict[str, str]]]) -> list[dict[str, str]]:
    deduped: list[tuple[int, dict[str, str]]] = []
    seen: set[str] = set()
    for score, job in sorted(candidates, key=lambda item: item[0], reverse=True):
        key = "|".join(
            [
                job.get("title", "").lower(),
                job.get("company", "").lower(),
                job.get("apply_link", "").lower(),
            ]
        )
        if not key.strip("|") or key in seen:
            continue
        seen.add(key)
        deduped.append((score, job))
        if len(deduped) >= 10:
            break
    return [job for _, job in deduped]


async def _fallback_search_jobs(query: str, country: str) -> list[dict[str, str]]:
    keywords, location = _split_query(query)
    keyword_terms = _tokens(keywords)
    location_terms = _tokens(location)
    country_terms = _country_terms(country)
    search_text = " ".join(keyword_terms) or keywords or query

    remotive_payload, arbeitnow_payload = await asyncio.gather(
        _get_json(REMOTIVE_URL, params={"search": search_text}),
        _get_json(ARBEITNOW_URL),
        return_exceptions=True,
    )

    candidates: list[tuple[int, dict[str, str]]] = []
    errors: list[str] = []
    if isinstance(remotive_payload, Exception):
        errors.append(f"Remotive: {remotive_payload}")
    else:
        candidates.extend(
            _extract_remotive_jobs(
                remotive_payload,
                keyword_terms,
                location_terms,
                country_terms,
            )
        )
    if isinstance(arbeitnow_payload, Exception):
        errors.append(f"Arbeitnow: {arbeitnow_payload}")
    else:
        candidates.extend(
            _extract_arbeitnow_jobs(
                arbeitnow_payload,
                keyword_terms,
                location_terms,
                country_terms,
            )
        )

    jobs = _merge_jobs(candidates)
    if jobs:
        return jobs
    if errors:
        raise RuntimeError("; ".join(errors))
    raise RuntimeError("Fallback jobs API natija topmadi.")


async def search_jobs(
    query: str,
    *,
    page: int = 1,
    num_pages: int = 1,
    country: str = "us",
    date_posted: str = "all",
) -> dict[str, Any]:
    clean_query = (query or "").strip()
    if not clean_query:
        raise ValueError("Ish qidiruvi uchun so'rov yuboring.")

    rapidapi_error: Exception | None = None
    try:
        payload = await rapidapi_get(
            host=JSEARCH_HOST,
            url=JSEARCH_URL,
            params={
                "query": clean_query,
                "page": max(1, int(page)),
                "num_pages": max(1, min(3, int(num_pages))),
                "country": (country or "us").strip().lower(),
                "date_posted": (date_posted or "all").strip().lower(),
            },
        )
        jobs = _extract_jobs(payload)
        if jobs:
            return {
                "query": clean_query,
                "jobs": jobs,
            }
    except Exception as error:  # noqa: BLE001
        rapidapi_error = error

    try:
        jobs = await _fallback_search_jobs(
            clean_query,
            (country or "us").strip().lower(),
        )
    except Exception as fallback_error:  # noqa: BLE001
        if rapidapi_error is not None:
            raise RuntimeError("Ish qidiruvi vaqtincha ishlamayapti.") from fallback_error
        raise

    return {
        "query": clean_query,
        "jobs": jobs,
    }
