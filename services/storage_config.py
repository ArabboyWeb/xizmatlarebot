from __future__ import annotations

import os
from urllib.parse import quote

DIRECT_DATABASE_ENV_KEYS = (
    "DATABASE_URL",
    "DATABASE_PRIVATE_URL",
    "DATABASE_PUBLIC_URL",
    "NEON_DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRESQL_URL",
)

HOSTED_ENV_KEYS = (
    "RAILWAY_PROJECT_ID",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_ENVIRONMENT_NAME",
    "RENDER",
    "KOYEB_SERVICE_ID",
    "FLY_APP_NAME",
    "HEROKU_APP_ID",
    "VERCEL",
)


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _env_flag(name: str) -> bool:
    value = _env(name).lower()
    return value in {"1", "true", "yes", "on"}


def _build_postgres_url_from_parts() -> str:
    host = _env("PGHOST") or _env("POSTGRES_HOST")
    port = _env("PGPORT") or _env("POSTGRES_PORT") or "5432"
    database = _env("PGDATABASE") or _env("POSTGRES_DB")
    username = _env("PGUSER") or _env("POSTGRES_USER")
    password = _env("PGPASSWORD") or _env("POSTGRES_PASSWORD")
    sslmode = _env("PGSSLMODE") or _env("POSTGRES_SSLMODE")
    if not host or not database or not username:
        return ""
    auth = quote(username, safe="")
    if password:
        auth = f"{auth}:{quote(password, safe='')}"
    dsn = f"postgresql://{auth}@{host}:{port}/{quote(database, safe='')}"
    if sslmode:
        dsn = f"{dsn}?sslmode={quote(sslmode, safe='')}"
    return dsn


def resolve_database_url(explicit: str | None = None) -> str:
    provided = str(explicit or "").strip()
    if provided:
        return provided
    for key in DIRECT_DATABASE_ENV_KEYS:
        value = _env(key)
        if value:
            return value
    return _build_postgres_url_from_parts()


def running_in_hosted_env() -> bool:
    return any(_env(key) for key in HOSTED_ENV_KEYS)


def should_require_persistent_database() -> bool:
    if _env_flag("ALLOW_EPHEMERAL_STORAGE"):
        return False
    if _env_flag("REQUIRE_DATABASE"):
        return True
    return running_in_hosted_env()
