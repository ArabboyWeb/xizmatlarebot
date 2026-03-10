from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware

from services.ai_gateway import MODEL_ALIAS_AUTO, clamp_selected_plan, effective_selected_plan
from services.token_pricing import (
    free_daily_tokens,
    free_reset_hours,
    normalize_plan,
    premium_monthly_tokens,
    referral_invitee_bonus,
    referral_inviter_bonus,
    resolve_service_key,
)

try:
    import asyncpg
except Exception:  # pragma: no cover
    asyncpg = None

DEFAULT_AI_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "ai_store.json"
COMPLIMENTARY_MEDIA_SERVICE_KEYS = {
    "youtube_download_video",
    "youtube_download_audio",
    "social_download",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return _utc_now()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return _utc_now()


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _plan_monthly_credits(plan: str) -> int:
    normalized = normalize_plan(plan)
    if normalized == "premium":
        return premium_monthly_tokens()
    return free_daily_tokens()


def _plan_rpm(plan: str) -> int:
    normalized = normalize_plan(plan)
    if normalized == "premium":
        return max(1, _read_int("AI_PREMIUM_RPM", 30))
    return max(1, _read_int("AI_FREE_RPM", 12))


def _free_daily_requests() -> int:
    return free_daily_tokens()


def _free_reset_tokens() -> int:
    return free_daily_tokens()


def _free_cooldown_seconds() -> int:
    return max(0, _read_int("AI_FREE_COOLDOWN_SECONDS", 5))


def _context_messages_limit() -> int:
    return max(4, _read_int("AI_CONTEXT_MESSAGES", 12))


def _normalize_selected_model(value: Any) -> str:
    normalized = str(value or MODEL_ALIAS_AUTO).strip().lower()
    return normalized or MODEL_ALIAS_AUTO


def _next_free_reset(now: datetime | None = None) -> datetime:
    base = now or _utc_now()
    interval = timedelta(hours=max(1, free_reset_hours()))
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    completed_steps = int((base - epoch) // interval)
    return epoch + interval * (completed_steps + 1)


def _next_monthly_reset(now: datetime | None = None) -> datetime:
    base = now or _utc_now()
    year = base.year + (1 if base.month == 12 else 0)
    month = 1 if base.month == 12 else base.month + 1
    return datetime(year=year, month=month, day=1, tzinfo=timezone.utc)


def _usage_event(
    *,
    provider: str,
    model: str,
    route: str,
    credits_used: int,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    ok: bool,
    error_text: str = "",
) -> dict[str, Any]:
    return {
        "requested_at": _iso(_utc_now()),
        "provider": provider,
        "model": model,
        "route": route,
        "credits_used": int(credits_used),
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "latency_ms": int(latency_ms),
        "ok": bool(ok),
        "error_text": str(error_text or "").strip(),
    }


def _complimentary_service_bucket(service_key: str) -> str:
    key = resolve_service_key(service_key)
    if key in COMPLIMENTARY_MEDIA_SERVICE_KEYS:
        return "media"
    return ""


class AIStore:
    def __init__(
        self,
        path: Path | None = None,
        database_url: str | None = None,
    ) -> None:
        self.path = path or DEFAULT_AI_DATA_PATH
        self.database_url = (
            database_url
            or os.getenv("DATABASE_URL", "").strip()
            or os.getenv("NEON_DATABASE_URL", "").strip()
        )
        self._lock = asyncio.Lock()
        self._loaded = False
        self._data: dict[str, Any] = {}
        self._pool: Any | None = None
        self._session_conversations: dict[str, list[dict[str, str]]] = {}

    def _default_data(self) -> dict[str, Any]:
        return {
            "users": {},
            "usage_history": {},
        }

    async def startup(self) -> None:
        if self.database_url:
            if asyncpg is None:
                raise RuntimeError("asyncpg o'rnatilmagan. AI store ishga tushmadi.")
            self._pool = await asyncpg.create_pool(
                dsn=self.database_url,
                min_size=1,
                max_size=5,
                command_timeout=60,
            )
            await self._ensure_schema()
            return
        await self._ensure_loaded()

    async def shutdown(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _ensure_schema(self) -> None:
        if self._pool is None:
            return
        schema_sql = """
        CREATE TABLE IF NOT EXISTS ai_users (
            user_id BIGINT PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            full_name TEXT NOT NULL DEFAULT '',
            current_plan TEXT NOT NULL DEFAULT 'free',
            selected_plan TEXT NOT NULL DEFAULT 'auto',
            selected_model TEXT NOT NULL DEFAULT 'auto',
            token_balance BIGINT NOT NULL DEFAULT 20,
            free_requests_used BIGINT NOT NULL DEFAULT 0,
            free_reset_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reset_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_request_at TIMESTAMPTZ,
            total_prompt_tokens BIGINT NOT NULL DEFAULT 0,
            total_completion_tokens BIGINT NOT NULL DEFAULT 0,
            referrer_id BIGINT,
            referral_count BIGINT NOT NULL DEFAULT 0,
            referral_bonus_claimed BOOLEAN NOT NULL DEFAULT FALSE,
            free_media_trial_used BOOLEAN NOT NULL DEFAULT FALSE,
            free_media_trial_cycle_end TIMESTAMPTZ,
            lifetime_tokens_earned BIGINT NOT NULL DEFAULT 0,
            lifetime_tokens_spent BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS ai_usage_events (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            route TEXT NOT NULL DEFAULT '',
            credits_used BIGINT NOT NULL DEFAULT 0,
            prompt_tokens BIGINT NOT NULL DEFAULT 0,
            completion_tokens BIGINT NOT NULL DEFAULT 0,
            latency_ms BIGINT NOT NULL DEFAULT 0,
            ok BOOLEAN NOT NULL DEFAULT TRUE,
            error_text TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_ai_usage_events_user_requested_at
            ON ai_usage_events (user_id, requested_at DESC);
        """
        async with self._pool.acquire() as connection:
            await connection.execute(schema_sql)
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS selected_plan TEXT NOT NULL DEFAULT 'auto';"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS selected_model TEXT NOT NULL DEFAULT 'auto';"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS referrer_id BIGINT;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS referral_count BIGINT NOT NULL DEFAULT 0;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS referral_bonus_claimed BOOLEAN NOT NULL DEFAULT FALSE;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS free_media_trial_used BOOLEAN NOT NULL DEFAULT FALSE;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS free_media_trial_cycle_end TIMESTAMPTZ;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS lifetime_tokens_earned BIGINT NOT NULL DEFAULT 0;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS lifetime_tokens_spent BIGINT NOT NULL DEFAULT 0;"
            )

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            if self.path.exists():
                try:
                    self._data = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:
                    self._data = self._default_data()
            else:
                self._data = self._default_data()
            self._loaded = True

    async def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.path)

    def _default_user(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        return {
            "user_id": int(user_id),
            "username": username,
            "full_name": full_name,
            "current_plan": "free",
            "selected_plan": MODEL_ALIAS_AUTO,
            "selected_model": MODEL_ALIAS_AUTO,
            "token_balance": _free_reset_tokens(),
            "free_requests_used": 0,
            "free_reset_date": _iso(_next_free_reset(now)),
            "reset_date": _iso(_next_free_reset(now)),
            "last_request_at": "",
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "referrer_id": 0,
            "referral_count": 0,
            "referral_bonus_claimed": False,
            "free_media_trial_used": False,
            "free_media_trial_cycle_end": _iso(_next_free_reset(now)),
            "lifetime_tokens_earned": 0,
            "lifetime_tokens_spent": 0,
            "created_at": _iso(now),
            "updated_at": _iso(now),
        }

    def _normalize_user_locked(
        self,
        user: dict[str, Any],
        *,
        username: str,
        full_name: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        if username:
            user["username"] = username
        if full_name:
            user["full_name"] = full_name
        current_plan = normalize_plan(str(user.get("current_plan", "free") or "free"))
        user["current_plan"] = current_plan
        user["selected_plan"] = clamp_selected_plan(
            str(user.get("selected_plan", MODEL_ALIAS_AUTO) or MODEL_ALIAS_AUTO),
            current_plan,
        )
        user["selected_model"] = _normalize_selected_model(
            user.get("selected_model", MODEL_ALIAS_AUTO)
        )

        free_reset_date = _parse_dt(user.get("free_reset_date"))
        if current_plan == "free":
            next_free_reset = _next_free_reset(now)
            if free_reset_date > next_free_reset:
                free_reset_date = next_free_reset
                user["free_reset_date"] = _iso(free_reset_date)
        if now >= free_reset_date:
            user["free_requests_used"] = 0
            free_reset_date = _next_free_reset(now)
            user["free_reset_date"] = _iso(free_reset_date)
            if current_plan == "free":
                user["token_balance"] = max(
                    int(user.get("token_balance", 0) or 0),
                    _free_reset_tokens(),
                )
                user["reset_date"] = _iso(free_reset_date)

        reset_date = _parse_dt(user.get("reset_date"))
        if current_plan == "free":
            if reset_date != free_reset_date:
                user["reset_date"] = _iso(free_reset_date)
        elif now >= reset_date:
            user["token_balance"] = max(
                int(user.get("token_balance", 0) or 0),
                _plan_monthly_credits(current_plan),
            )
            user["reset_date"] = _iso(_next_monthly_reset(now))

        user["referrer_id"] = int(user.get("referrer_id", 0) or 0)
        user["referral_count"] = max(0, int(user.get("referral_count", 0) or 0))
        user["referral_bonus_claimed"] = bool(user.get("referral_bonus_claimed", False))
        current_free_cycle = str(user.get("free_reset_date", "") or "")
        if str(user.get("free_media_trial_cycle_end", "") or "") != current_free_cycle:
            user["free_media_trial_cycle_end"] = current_free_cycle
            user["free_media_trial_used"] = False
        else:
            user["free_media_trial_used"] = bool(user.get("free_media_trial_used", False))
        user["lifetime_tokens_earned"] = max(
            0, int(user.get("lifetime_tokens_earned", 0) or 0)
        )
        user["lifetime_tokens_spent"] = max(
            0, int(user.get("lifetime_tokens_spent", 0) or 0)
        )
        user["updated_at"] = _iso(now)
        return user

    async def ensure_user(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
    ) -> dict[str, Any]:
        if self._pool is not None:
            return await self._db_ensure_user(
                user_id=user_id,
                username=username,
                full_name=full_name,
            )

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            key = str(int(user_id))
            user = users.get(key)
            if not isinstance(user, dict):
                user = self._default_user(
                    user_id=user_id,
                    username=username,
                    full_name=full_name,
                )
                users[key] = user
            normalized = self._normalize_user_locked(
                user,
                username=username,
                full_name=full_name,
            )
            await self._save_locked()
            return json.loads(json.dumps(normalized))

    async def _db_ensure_user(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
    ) -> dict[str, Any]:
        if self._pool is None:
            raise RuntimeError("AI database pool topilmadi.")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    INSERT INTO ai_users (
                        user_id,
                        username,
                        full_name,
                        current_plan,
                        token_balance,
                        free_requests_used,
                        free_reset_date,
                        reset_date,
                        created_at,
                        updated_at
                    )
                    VALUES ($1, $2, $3, 'free', $4, 0, $5, $5, NOW(), NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        username = CASE
                            WHEN EXCLUDED.username <> '' THEN EXCLUDED.username
                            ELSE ai_users.username
                        END,
                        full_name = CASE
                            WHEN EXCLUDED.full_name <> '' THEN EXCLUDED.full_name
                            ELSE ai_users.full_name
                        END,
                        updated_at = NOW()
                    RETURNING *
                    """,
                    int(user_id),
                    username,
                    full_name,
                    _free_reset_tokens(),
                    _next_free_reset(),
                )
                if row is None:
                    raise RuntimeError("AI foydalanuvchi yozuvi yaratilmadi.")
                return await self._db_normalize_user_locked(
                    connection,
                    row=row,
                    username=username,
                    full_name=full_name,
                )

    async def _db_normalize_user_locked(
        self,
        connection: Any,
        *,
        row: Any,
        username: str,
        full_name: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        current_plan = str(row["current_plan"] or "free").strip().lower()
        current_plan = normalize_plan(current_plan)
        token_balance = int(row["token_balance"] or 0)
        free_requests_used = int(row["free_requests_used"] or 0)
        free_reset_date = _parse_dt(row["free_reset_date"])
        reset_date = _parse_dt(row["reset_date"])

        if current_plan == "free":
            next_free_reset = _next_free_reset(now)
            if free_reset_date > next_free_reset:
                free_reset_date = next_free_reset
                reset_date = next_free_reset
                await connection.execute(
                    """
                    UPDATE ai_users
                    SET free_reset_date = $2,
                        reset_date = $2,
                        updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    int(row["user_id"]),
                    free_reset_date,
                )

        if now >= free_reset_date:
            free_requests_used = 0
            free_reset_date = _next_free_reset(now)
            if current_plan == "free":
                token_balance = max(token_balance, _free_reset_tokens())
                reset_date = free_reset_date
                await connection.execute(
                    """
                    UPDATE ai_users
                    SET free_requests_used = 0,
                        free_reset_date = $2,
                        token_balance = $3,
                        reset_date = $2,
                        updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    int(row["user_id"]),
                    free_reset_date,
                    token_balance,
                )
            else:
                await connection.execute(
                    """
                    UPDATE ai_users
                    SET free_requests_used = 0,
                        free_reset_date = $2,
                        updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    int(row["user_id"]),
                    free_reset_date,
                )
        elif current_plan == "free" and reset_date != free_reset_date:
            reset_date = free_reset_date
            await connection.execute(
                """
                UPDATE ai_users
                SET reset_date = $2,
                    updated_at = NOW()
                WHERE user_id = $1
                """,
                int(row["user_id"]),
                reset_date,
            )
        if current_plan != "free" and now >= reset_date:
            token_balance = max(token_balance, _plan_monthly_credits(current_plan))
            reset_date = _next_monthly_reset(now)
            await connection.execute(
                """
                UPDATE ai_users
                SET token_balance = $2,
                    reset_date = $3,
                    updated_at = NOW()
                WHERE user_id = $1
                """,
                int(row["user_id"]),
                token_balance,
                reset_date,
            )

        trial_cycle_end_raw = row["free_media_trial_cycle_end"]
        trial_cycle_end = (
            _iso(_parse_dt(trial_cycle_end_raw)) if trial_cycle_end_raw else ""
        )
        current_cycle_end = _iso(free_reset_date)
        if trial_cycle_end != current_cycle_end:
            await connection.execute(
                """
                UPDATE ai_users
                SET free_media_trial_used = FALSE,
                    free_media_trial_cycle_end = $2,
                    updated_at = NOW()
                WHERE user_id = $1
                """,
                int(row["user_id"]),
                free_reset_date,
            )

        final_row = await connection.fetchrow(
            "SELECT * FROM ai_users WHERE user_id = $1",
            int(row["user_id"]),
        )
        if final_row is None:
            raise RuntimeError("AI foydalanuvchi yozuvi topilmadi.")
        return self._serialize_row(final_row)

    def _serialize_row(self, row: Any) -> dict[str, Any]:
        return {
            "user_id": int(row["user_id"]),
            "username": str(row["username"] or ""),
            "full_name": str(row["full_name"] or ""),
            "current_plan": normalize_plan(str(row["current_plan"] or "free")),
            "selected_plan": str(row["selected_plan"] or MODEL_ALIAS_AUTO),
            "selected_model": str(row["selected_model"] or MODEL_ALIAS_AUTO),
            "token_balance": int(row["token_balance"] or 0),
            "free_requests_used": int(row["free_requests_used"] or 0),
            "free_reset_date": _iso(_parse_dt(row["free_reset_date"])),
            "reset_date": _iso(_parse_dt(row["reset_date"])),
            "last_request_at": (
                _iso(_parse_dt(row["last_request_at"]))
                if row["last_request_at"]
                else ""
            ),
            "total_prompt_tokens": int(row["total_prompt_tokens"] or 0),
            "total_completion_tokens": int(row["total_completion_tokens"] or 0),
            "referrer_id": int(row["referrer_id"] or 0),
            "referral_count": int(row["referral_count"] or 0),
            "referral_bonus_claimed": bool(row["referral_bonus_claimed"]),
            "free_media_trial_used": bool(row["free_media_trial_used"]),
            "free_media_trial_cycle_end": (
                _iso(_parse_dt(row["free_media_trial_cycle_end"]))
                if row["free_media_trial_cycle_end"]
                else ""
            ),
            "lifetime_tokens_earned": int(row["lifetime_tokens_earned"] or 0),
            "lifetime_tokens_spent": int(row["lifetime_tokens_spent"] or 0),
            "created_at": _iso(_parse_dt(row["created_at"])),
            "updated_at": _iso(_parse_dt(row["updated_at"])),
        }

    def _effective_plan(self, user: dict[str, Any]) -> str:
        return effective_selected_plan(user)

    async def requests_in_last_minute(self, *, user_id: int) -> int:
        threshold = _utc_now() - timedelta(minutes=1)
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                value = await connection.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM ai_usage_events
                    WHERE user_id = $1 AND requested_at >= $2
                    """,
                    int(user_id),
                    threshold,
                )
            return int(value or 0)

        await self._ensure_loaded()
        async with self._lock:
            history = self._data.setdefault("usage_history", {}).get(str(int(user_id)), [])
            if not isinstance(history, list):
                return 0
            count = 0
            for item in history:
                if not isinstance(item, dict):
                    continue
                requested_at = _parse_dt(item.get("requested_at"))
                if requested_at >= threshold:
                    count += 1
            return count

    async def check_request_limits(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
    ) -> tuple[dict[str, Any], str, int]:
        user = await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        effective_plan = self._effective_plan(user)
        now = _utc_now()
        wait_seconds = 0

        last_request_at = str(user.get("last_request_at", "") or "").strip()
        if effective_plan == "free" and last_request_at:
            last_dt = _parse_dt(last_request_at)
            elapsed = (now - last_dt).total_seconds()
            cooldown = _free_cooldown_seconds()
            if elapsed < cooldown:
                wait_seconds = max(wait_seconds, int(cooldown - elapsed + 0.999))

        token_balance = int(user.get("token_balance", 0) or 0)
        if token_balance <= 0:
            if effective_plan == "free":
                reset_at = _parse_dt(user.get("free_reset_date"))
            else:
                reset_at = _parse_dt(user.get("reset_date"))
            wait_seconds = max(
                wait_seconds,
                int((reset_at - now).total_seconds() + 0.999),
            )

        rpm = _plan_rpm(effective_plan)
        recent_count = await self.requests_in_last_minute(user_id=user_id)
        if recent_count >= rpm:
            wait_seconds = max(wait_seconds, 60)

        return user, effective_plan, max(0, wait_seconds)

    async def record_usage(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        effective_plan: str,
        provider: str,
        model: str,
        route: str,
        credits_used: int,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        ok: bool,
        error_text: str = "",
    ) -> dict[str, Any]:
        if self._pool is not None:
            return await self._db_record_usage(
                user_id=user_id,
                username=username,
                full_name=full_name,
                effective_plan=effective_plan,
                provider=provider,
                model=model,
                route=route,
                credits_used=credits_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                ok=ok,
                error_text=error_text,
            )

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            key = str(int(user_id))
            user = users.get(key)
            if not isinstance(user, dict):
                user = self._default_user(
                    user_id=user_id,
                    username=username,
                    full_name=full_name,
                )
                users[key] = user
            self._normalize_user_locked(user, username=username, full_name=full_name)

            if ok:
                if effective_plan == "free":
                    user["free_requests_used"] = int(user.get("free_requests_used", 0) or 0) + 1
                user["token_balance"] = max(
                    0,
                    int(user.get("token_balance", 0) or 0) - int(credits_used),
                )
                user["lifetime_tokens_spent"] = int(
                    user.get("lifetime_tokens_spent", 0) or 0
                ) + int(credits_used)
                user["total_prompt_tokens"] = int(user.get("total_prompt_tokens", 0) or 0) + int(prompt_tokens)
                user["total_completion_tokens"] = int(user.get("total_completion_tokens", 0) or 0) + int(completion_tokens)

            user["last_request_at"] = _iso(_utc_now())
            user["updated_at"] = _iso(_utc_now())

            history = self._data.setdefault("usage_history", {}).setdefault(key, [])
            if isinstance(history, list):
                history.insert(
                    0,
                    _usage_event(
                        provider=provider,
                        model=model,
                        route=route,
                        credits_used=credits_used,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        latency_ms=latency_ms,
                        ok=ok,
                        error_text=error_text,
                    ),
                )
                del history[200:]
            await self._save_locked()
            return json.loads(json.dumps(user))

    async def _db_record_usage(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        effective_plan: str,
        provider: str,
        model: str,
        route: str,
        credits_used: int,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        ok: bool,
        error_text: str,
    ) -> dict[str, Any]:
        if self._pool is None:
            raise RuntimeError("AI database pool topilmadi.")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._db_ensure_user(
                    user_id=user_id,
                    username=username,
                    full_name=full_name,
                )
                if ok:
                    if effective_plan == "free":
                        await connection.execute(
                            """
                            UPDATE ai_users
                            SET free_requests_used = free_requests_used + 1,
                                token_balance = GREATEST(0, token_balance - $2),
                                lifetime_tokens_spent = lifetime_tokens_spent + $2,
                                total_prompt_tokens = total_prompt_tokens + $3,
                                total_completion_tokens = total_completion_tokens + $4,
                                last_request_at = NOW(),
                                updated_at = NOW()
                            WHERE user_id = $1
                            """,
                            int(user_id),
                            int(credits_used),
                            int(prompt_tokens),
                            int(completion_tokens),
                        )
                    else:
                        await connection.execute(
                            """
                            UPDATE ai_users
                            SET token_balance = GREATEST(0, token_balance - $2),
                                lifetime_tokens_spent = lifetime_tokens_spent + $2,
                                total_prompt_tokens = total_prompt_tokens + $3,
                                total_completion_tokens = total_completion_tokens + $4,
                                last_request_at = NOW(),
                                updated_at = NOW()
                            WHERE user_id = $1
                            """,
                            int(user_id),
                            int(credits_used),
                            int(prompt_tokens),
                            int(completion_tokens),
                        )
                else:
                    await connection.execute(
                        """
                        UPDATE ai_users
                        SET last_request_at = NOW(),
                            updated_at = NOW()
                        WHERE user_id = $1
                        """,
                        int(user_id),
                    )

                await connection.execute(
                    """
                    INSERT INTO ai_usage_events (
                        user_id,
                        requested_at,
                        provider,
                        model,
                        route,
                        credits_used,
                        prompt_tokens,
                        completion_tokens,
                        latency_ms,
                        ok,
                        error_text
                    )
                    VALUES ($1, NOW(), $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    int(user_id),
                    provider,
                    model,
                    route,
                    int(credits_used),
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(latency_ms),
                    bool(ok),
                    error_text,
                )

                row = await connection.fetchrow(
                    "SELECT * FROM ai_users WHERE user_id = $1",
                    int(user_id),
                )
                if row is None:
                    raise RuntimeError("AI foydalanuvchi yozuvi yangilanmadi.")
                return self._serialize_row(row)

    async def get_conversation(self, *, user_id: int) -> list[dict[str, str]]:
        history = self._session_conversations.get(str(int(user_id)), [])
        if not isinstance(history, list):
            return []
        return json.loads(json.dumps(history))

    async def append_conversation_turn(
        self,
        *,
        user_id: int,
        user_text: str,
        assistant_text: str,
    ) -> None:
        max_messages = _context_messages_limit()
        user_turn = {"role": "user", "content": str(user_text or "").strip()}
        assistant_turn = {"role": "assistant", "content": str(assistant_text or "").strip()}
        key = str(int(user_id))
        history = self._session_conversations.setdefault(key, [])
        if not isinstance(history, list):
            history = []
            self._session_conversations[key] = history
        history.extend([user_turn, assistant_turn])
        del history[:-max_messages]

    async def clear_conversation(self, *, user_id: int) -> None:
        self._session_conversations.pop(str(int(user_id)), None)

    async def set_user_plan(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        plan: str,
        credits: int | None = None,
    ) -> dict[str, Any]:
        normalized_plan = normalize_plan(plan)
        if normalized_plan not in {"free", "premium"}:
            raise ValueError("Plan faqat free yoki premium bo'lishi kerak.")
        await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        now = _utc_now()
        if normalized_plan == "free":
            target_balance = _free_reset_tokens()
            target_reset = _next_free_reset(now)
        else:
            target_balance = (
                int(credits)
                if isinstance(credits, int) and credits > 0
                else _plan_monthly_credits(normalized_plan)
            )
            target_reset = _next_monthly_reset(now)

        if self._pool is not None:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    """
                    UPDATE ai_users
                    SET current_plan = $2,
                        selected_plan = 'auto',
                        selected_model = 'auto',
                        token_balance = $3,
                        free_requests_used = 0,
                        free_reset_date = $4,
                        reset_date = $5,
                        free_media_trial_used = FALSE,
                        free_media_trial_cycle_end = $4,
                        updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    int(user_id),
                    normalized_plan,
                    int(target_balance),
                    _next_free_reset(now),
                    target_reset,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM ai_users WHERE user_id = $1",
                    int(user_id),
                )
            if row is None:
                raise RuntimeError("AI foydalanuvchi plani yangilanmadi.")
            return self._serialize_row(row)

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            record = users.setdefault(
                str(int(user_id)),
                self._default_user(
                    user_id=user_id,
                    username=username,
                    full_name=full_name,
                ),
            )
            record["current_plan"] = normalized_plan
            record["selected_plan"] = MODEL_ALIAS_AUTO
            record["selected_model"] = MODEL_ALIAS_AUTO
            record["token_balance"] = int(target_balance)
            record["free_requests_used"] = 0
            record["free_reset_date"] = _iso(_next_free_reset(now))
            record["reset_date"] = _iso(target_reset)
            record["free_media_trial_used"] = False
            record["free_media_trial_cycle_end"] = record["free_reset_date"]
            record["updated_at"] = _iso(now)
            await self._save_locked()
            return json.loads(json.dumps(record))

    async def set_user_credits(
        self,
        *,
        user_id: int,
        credits: int,
    ) -> dict[str, Any]:
        if credits < 0:
            raise ValueError("Kredit manfiy bo'lishi mumkin emas.")
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    """
                    UPDATE ai_users
                    SET token_balance = $2,
                        updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    int(user_id),
                    int(credits),
                )
                row = await connection.fetchrow(
                    "SELECT * FROM ai_users WHERE user_id = $1",
                    int(user_id),
                )
            if row is None:
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            return self._serialize_row(row)

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            record = users.get(str(int(user_id)))
            if not isinstance(record, dict):
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            record["token_balance"] = int(credits)
            record["updated_at"] = _iso(_utc_now())
            await self._save_locked()
            return json.loads(json.dumps(record))

    async def set_user_selected_plan(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        selected_plan: str,
    ) -> dict[str, Any]:
        user = await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        normalized_selected_plan = clamp_selected_plan(
            selected_plan,
            str(user.get("current_plan", "free") or "free"),
        )
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    """
                    UPDATE ai_users
                    SET selected_plan = $2,
                        selected_model = 'auto',
                        updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    int(user_id),
                    normalized_selected_plan,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM ai_users WHERE user_id = $1",
                    int(user_id),
                )
            if row is None:
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            return self._serialize_row(row)

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            record = users.get(str(int(user_id)))
            if not isinstance(record, dict):
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            record["selected_plan"] = normalized_selected_plan
            record["selected_model"] = MODEL_ALIAS_AUTO
            record["updated_at"] = _iso(_utc_now())
            await self._save_locked()
            return json.loads(json.dumps(record))

    async def set_user_selected_model(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        selected_model: str,
    ) -> dict[str, Any]:
        await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        normalized_selected_model = _normalize_selected_model(selected_model)
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    """
                    UPDATE ai_users
                    SET selected_model = $2,
                        updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    int(user_id),
                    normalized_selected_model,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM ai_users WHERE user_id = $1",
                    int(user_id),
                )
            if row is None:
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            return self._serialize_row(row)

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            record = users.get(str(int(user_id)))
            if not isinstance(record, dict):
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            record["selected_model"] = normalized_selected_model
            record["updated_at"] = _iso(_utc_now())
            await self._save_locked()
            return json.loads(json.dumps(record))

    async def can_use_complimentary_service(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        service_key: str,
        user: dict[str, Any] | None = None,
    ) -> bool:
        if _complimentary_service_bucket(service_key) != "media":
            return False
        record = user or await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        if normalize_plan(str(record.get("current_plan", "free") or "free")) != "free":
            return False
        current_cycle = str(record.get("free_reset_date", "") or "")
        trial_cycle = str(record.get("free_media_trial_cycle_end", "") or "")
        if trial_cycle != current_cycle:
            return True
        return not bool(record.get("free_media_trial_used", False))

    async def consume_complimentary_service(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        service_key: str,
    ) -> dict[str, Any]:
        if _complimentary_service_bucket(service_key) != "media":
            return await self.ensure_user(
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
        await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        "SELECT * FROM ai_users WHERE user_id = $1 FOR UPDATE",
                        int(user_id),
                    )
                    if row is None:
                        raise RuntimeError("AI foydalanuvchi topilmadi.")
                    current_plan = normalize_plan(str(row["current_plan"] or "free"))
                    if current_plan == "free":
                        free_reset_date = _parse_dt(row["free_reset_date"])
                        trial_cycle_end = (
                            _iso(_parse_dt(row["free_media_trial_cycle_end"]))
                            if row["free_media_trial_cycle_end"]
                            else ""
                        )
                        current_cycle_end = _iso(free_reset_date)
                        used = bool(row["free_media_trial_used"])
                        if trial_cycle_end != current_cycle_end or not used:
                            await connection.execute(
                                """
                                UPDATE ai_users
                                SET free_media_trial_used = TRUE,
                                    free_media_trial_cycle_end = $2,
                                    updated_at = NOW()
                                WHERE user_id = $1
                                """,
                                int(user_id),
                                free_reset_date,
                            )
                    updated = await connection.fetchrow(
                        "SELECT * FROM ai_users WHERE user_id = $1",
                        int(user_id),
                    )
                if updated is None:
                    raise RuntimeError("AI foydalanuvchi topilmadi.")
                return self._serialize_row(updated)

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            record = users.get(str(int(user_id)))
            if not isinstance(record, dict):
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            self._normalize_user_locked(record, username=username, full_name=full_name)
            if normalize_plan(str(record.get("current_plan", "free") or "free")) == "free":
                record["free_media_trial_used"] = True
                record["free_media_trial_cycle_end"] = str(
                    record.get("free_reset_date", "") or ""
                )
                record["updated_at"] = _iso(_utc_now())
                await self._save_locked()
            return json.loads(json.dumps(record))

    async def charge_tokens(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        amount: int,
    ) -> dict[str, Any]:
        token_amount = max(0, int(amount))
        user = await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        if token_amount <= 0:
            return user

        if self._pool is not None:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        "SELECT * FROM ai_users WHERE user_id = $1 FOR UPDATE",
                        int(user_id),
                    )
                    if row is None:
                        raise RuntimeError("AI foydalanuvchi topilmadi.")
                    balance = int(row["token_balance"] or 0)
                    if balance < token_amount:
                        raise ValueError("Token yetarli emas.")
                    await connection.execute(
                        """
                        UPDATE ai_users
                        SET token_balance = token_balance - $2,
                            lifetime_tokens_spent = lifetime_tokens_spent + $2,
                            updated_at = NOW()
                        WHERE user_id = $1
                        """,
                        int(user_id),
                        token_amount,
                    )
                    updated = await connection.fetchrow(
                        "SELECT * FROM ai_users WHERE user_id = $1",
                        int(user_id),
                    )
                if updated is None:
                    raise RuntimeError("AI foydalanuvchi topilmadi.")
                return self._serialize_row(updated)

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            record = users.get(str(int(user_id)))
            if not isinstance(record, dict):
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            self._normalize_user_locked(record, username=username, full_name=full_name)
            if int(record.get("token_balance", 0) or 0) < token_amount:
                raise ValueError("Token yetarli emas.")
            record["token_balance"] = int(record.get("token_balance", 0) or 0) - token_amount
            record["lifetime_tokens_spent"] = int(
                record.get("lifetime_tokens_spent", 0) or 0
            ) + token_amount
            record["updated_at"] = _iso(_utc_now())
            await self._save_locked()
            return json.loads(json.dumps(record))

    async def award_tokens(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        amount: int,
    ) -> dict[str, Any]:
        token_amount = max(0, int(amount))
        user = await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        if token_amount <= 0:
            return user

        if self._pool is not None:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    """
                    UPDATE ai_users
                    SET token_balance = token_balance + $2,
                        lifetime_tokens_earned = lifetime_tokens_earned + $2,
                        updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    int(user_id),
                    token_amount,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM ai_users WHERE user_id = $1",
                    int(user_id),
                )
            if row is None:
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            return self._serialize_row(row)

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            record = users.get(str(int(user_id)))
            if not isinstance(record, dict):
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            self._normalize_user_locked(record, username=username, full_name=full_name)
            record["token_balance"] = int(record.get("token_balance", 0) or 0) + token_amount
            record["lifetime_tokens_earned"] = int(
                record.get("lifetime_tokens_earned", 0) or 0
            ) + token_amount
            record["updated_at"] = _iso(_utc_now())
            await self._save_locked()
            return json.loads(json.dumps(record))

    async def apply_referral(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        referrer_id: int,
    ) -> dict[str, Any]:
        if not isinstance(referrer_id, int) or referrer_id <= 0 or referrer_id == user_id:
            return {"applied": False, "reason": "invalid"}

        inviter_bonus = referral_inviter_bonus()
        invitee_bonus = referral_invitee_bonus()
        await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        await self.ensure_user(
            user_id=referrer_id,
            username="",
            full_name="",
        )

        if self._pool is not None:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    referred = await connection.fetchrow(
                        "SELECT * FROM ai_users WHERE user_id = $1 FOR UPDATE",
                        int(user_id),
                    )
                    referrer = await connection.fetchrow(
                        "SELECT * FROM ai_users WHERE user_id = $1 FOR UPDATE",
                        int(referrer_id),
                    )
                    if referred is None or referrer is None:
                        return {"applied": False, "reason": "missing"}
                    if int(referred["user_id"]) == int(referrer["user_id"]):
                        return {"applied": False, "reason": "self"}
                    if int(referred["referrer_id"] or 0) > 0 or bool(
                        referred["referral_bonus_claimed"]
                    ):
                        return {"applied": False, "reason": "exists"}

                    await connection.execute(
                        """
                        UPDATE ai_users
                        SET referrer_id = $2,
                            referral_bonus_claimed = TRUE,
                            token_balance = token_balance + $3,
                            lifetime_tokens_earned = lifetime_tokens_earned + $3,
                            updated_at = NOW()
                        WHERE user_id = $1
                        """,
                        int(user_id),
                        int(referrer_id),
                        int(invitee_bonus),
                    )
                    await connection.execute(
                        """
                        UPDATE ai_users
                        SET referral_count = referral_count + 1,
                            token_balance = token_balance + $2,
                            lifetime_tokens_earned = lifetime_tokens_earned + $2,
                            updated_at = NOW()
                        WHERE user_id = $1
                        """,
                        int(referrer_id),
                        int(inviter_bonus),
                    )
            return {
                "applied": True,
                "inviter_bonus": inviter_bonus,
                "invitee_bonus": invitee_bonus,
            }

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            referred = users.get(str(int(user_id)))
            referrer = users.get(str(int(referrer_id)))
            if not isinstance(referred, dict) or not isinstance(referrer, dict):
                return {"applied": False, "reason": "missing"}
            self._normalize_user_locked(referred, username=username, full_name=full_name)
            self._normalize_user_locked(referrer, username="", full_name="")
            if int(referred.get("referrer_id", 0) or 0) > 0 or bool(
                referred.get("referral_bonus_claimed", False)
            ):
                return {"applied": False, "reason": "exists"}

            referred["referrer_id"] = int(referrer_id)
            referred["referral_bonus_claimed"] = True
            referred["token_balance"] = int(referred.get("token_balance", 0) or 0) + invitee_bonus
            referred["lifetime_tokens_earned"] = int(
                referred.get("lifetime_tokens_earned", 0) or 0
            ) + invitee_bonus
            referred["updated_at"] = _iso(_utc_now())

            referrer["referral_count"] = int(referrer.get("referral_count", 0) or 0) + 1
            referrer["token_balance"] = int(referrer.get("token_balance", 0) or 0) + inviter_bonus
            referrer["lifetime_tokens_earned"] = int(
                referrer.get("lifetime_tokens_earned", 0) or 0
            ) + inviter_bonus
            referrer["updated_at"] = _iso(_utc_now())
            await self._save_locked()
            return {
                "applied": True,
                "inviter_bonus": inviter_bonus,
                "invitee_bonus": invitee_bonus,
            }


class AIContextMiddleware(BaseMiddleware):
    def __init__(self, ai_store: AIStore) -> None:
        self.ai_store = ai_store

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        data["ai_store"] = self.ai_store
        return await handler(event, data)
