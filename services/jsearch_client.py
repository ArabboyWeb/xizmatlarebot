from typing import Any

from services.rapidapi_client import rapidapi_get

JSEARCH_HOST = "jsearch.p.rapidapi.com"
JSEARCH_URL = "https://jsearch.p.rapidapi.com/search"


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
    return {
        "query": clean_query,
        "jobs": jobs,
    }
