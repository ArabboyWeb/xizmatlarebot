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
    free_signup_tokens,
    normalize_plan,
    premium_daily_tokens,
    premium_upgrade_tokens,
    referral_invitee_bonus,
    referral_inviter_bonus,
    refill_interval_hours,
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


def _plan_rpm(plan: str) -> int:
    normalized = normalize_plan(plan)
    if normalized == "premium":
        return max(1, _read_int("AI_PREMIUM_RPM", 60))
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


def _refill_interval() -> timedelta:
    return timedelta(hours=max(1, refill_interval_hours()))


def _next_token_refill(now: datetime | None = None) -> datetime:
    base = now or _utc_now()
    return base + _refill_interval()


def _refill_amount(plan: str) -> int:
    if normalize_plan(plan) == "premium":
        return premium_daily_tokens()
    return free_daily_tokens()


def _referral_claim_window_minutes() -> int:
    return max(1, _read_int("BOT_REFERRAL_CLAIM_WINDOW_MINUTES", 10))


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
        self._recent_requests: dict[str, list[datetime]] = {}

    def _default_data(self) -> dict[str, Any]:
        return {
            "users": {},
            "premium_requests": {},
            "premium_request_sequence": 0,
        }

    async def startup(self) -> None:
        if self.database_url:
            if asyncpg is None:
                raise RuntimeError("asyncpg o'rnatilmagan. AI store ishga tushmadi.")
            min_size = max(1, _read_int("DB_POOL_MIN_SIZE", 2))
            max_size = max(min_size, _read_int("DB_POOL_MAX_SIZE", 20))
            self._pool = await asyncpg.create_pool(
                dsn=self.database_url,
                min_size=min_size,
                max_size=max_size,
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
            token_balance BIGINT NOT NULL DEFAULT 100,
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
        CREATE TABLE IF NOT EXISTS premium_requests (
            request_id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            full_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            screenshot_file_id TEXT NOT NULL DEFAULT '',
            screenshot_file_unique_id TEXT NOT NULL DEFAULT '',
            screenshot_type TEXT NOT NULL DEFAULT '',
            submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reviewed_at TIMESTAMPTZ,
            reviewed_by BIGINT,
            reviewer_note TEXT NOT NULL DEFAULT '',
            admin_message_refs JSONB NOT NULL DEFAULT '[]'::jsonb
        );
        CREATE INDEX IF NOT EXISTS idx_premium_requests_user_id
            ON premium_requests (user_id);
        CREATE INDEX IF NOT EXISTS idx_premium_requests_status_submitted
            ON premium_requests (status, submitted_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_premium_requests_active_user
            ON premium_requests (user_id)
            WHERE status = 'pending';
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
            await connection.execute(
                "ALTER TABLE premium_requests ADD COLUMN IF NOT EXISTS reviewer_note TEXT NOT NULL DEFAULT '';"
            )
            await connection.execute(
                "ALTER TABLE premium_requests ADD COLUMN IF NOT EXISTS admin_message_refs JSONB NOT NULL DEFAULT '[]'::jsonb;"
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
            if not isinstance(self._data, dict):
                self._data = self._default_data()
            self._data.setdefault("users", {})
            self._data.setdefault("premium_requests", {})
            self._data["premium_request_sequence"] = int(
                self._data.get("premium_request_sequence", 0) or 0
            )
            self._data.pop("usage_history", None)
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
        next_refill = _next_token_refill(now)
        signup_tokens = free_signup_tokens()
        return {
            "user_id": int(user_id),
            "username": username,
            "full_name": full_name,
            "current_plan": "free",
            "selected_plan": MODEL_ALIAS_AUTO,
            "selected_model": MODEL_ALIAS_AUTO,
            "token_balance": signup_tokens,
            "free_requests_used": 0,
            "free_reset_date": _iso(next_refill),
            "reset_date": _iso(next_refill),
            "last_request_at": "",
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "referrer_id": 0,
            "referral_count": 0,
            "referral_bonus_claimed": False,
            "free_media_trial_used": False,
            "free_media_trial_cycle_end": _iso(next_refill),
            "lifetime_tokens_earned": signup_tokens,
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
        token_balance = max(0, int(user.get("token_balance", 0) or 0))
        lifetime_tokens_earned = max(0, int(user.get("lifetime_tokens_earned", 0) or 0))
        refill_default = _next_token_refill(now)
        reset_raw = user.get("reset_date", "")
        free_reset_raw = user.get("free_reset_date", "")
        if current_plan == "premium":
            refill_at = (
                _parse_dt(reset_raw)
                if str(reset_raw or "").strip()
                else (
                    _parse_dt(free_reset_raw)
                    if str(free_reset_raw or "").strip()
                    else refill_default
                )
            )
        else:
            refill_at = (
                _parse_dt(free_reset_raw)
                if str(free_reset_raw or "").strip()
                else (
                    _parse_dt(reset_raw)
                    if str(reset_raw or "").strip()
                    else refill_default
                )
            )
        if now >= refill_at:
            token_balance += _refill_amount(current_plan)
            lifetime_tokens_earned += _refill_amount(current_plan)
            user["free_requests_used"] = 0
            refill_at = _next_token_refill(now)
        user["token_balance"] = token_balance
        user["lifetime_tokens_earned"] = lifetime_tokens_earned
        user["free_reset_date"] = _iso(refill_at)
        user["reset_date"] = _iso(refill_at)

        user["referrer_id"] = int(user.get("referrer_id", 0) or 0)
        user["referral_count"] = max(0, int(user.get("referral_count", 0) or 0))
        user["referral_bonus_claimed"] = bool(user.get("referral_bonus_claimed", False))
        current_free_cycle = str(user.get("free_reset_date", "") or "")
        if str(user.get("free_media_trial_cycle_end", "") or "") != current_free_cycle:
            user["free_media_trial_cycle_end"] = current_free_cycle
            user["free_media_trial_used"] = False
        else:
            user["free_media_trial_used"] = bool(user.get("free_media_trial_used", False))
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
                        lifetime_tokens_earned,
                        created_at,
                        updated_at
                    )
                    VALUES ($1, $2, $3, 'free', $4, 0, $5, $5, $4, NOW(), NOW())
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
                    free_signup_tokens(),
                    _next_token_refill(),
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
        lifetime_tokens_earned = int(row["lifetime_tokens_earned"] or 0)
        refill_default = _next_token_refill(now)
        reset_raw = row["reset_date"]
        free_reset_raw = row["free_reset_date"]
        if current_plan == "premium":
            refill_at = (
                _parse_dt(reset_raw)
                if reset_raw
                else (_parse_dt(free_reset_raw) if free_reset_raw else refill_default)
            )
        else:
            refill_at = (
                _parse_dt(free_reset_raw)
                if free_reset_raw
                else (_parse_dt(reset_raw) if reset_raw else refill_default)
            )

        changed = False
        if now >= refill_at:
            refill_amount = _refill_amount(current_plan)
            token_balance += refill_amount
            lifetime_tokens_earned += refill_amount
            free_requests_used = 0
            refill_at = _next_token_refill(now)
            changed = True

        if (
            changed
            or _iso(_parse_dt(row["free_reset_date"])) != _iso(refill_at)
            or _iso(_parse_dt(row["reset_date"])) != _iso(refill_at)
            or int(row["token_balance"] or 0) != token_balance
            or int(row["lifetime_tokens_earned"] or 0) != lifetime_tokens_earned
            or int(row["free_requests_used"] or 0) != free_requests_used
        ):
            await connection.execute(
                """
                UPDATE ai_users
                SET free_requests_used = $2,
                    free_reset_date = $3,
                    reset_date = $3,
                    token_balance = $4,
                    lifetime_tokens_earned = $5,
                    updated_at = NOW()
                WHERE user_id = $1
                """,
                int(row["user_id"]),
                free_requests_used,
                refill_at,
                token_balance,
                lifetime_tokens_earned,
            )

        trial_cycle_end_raw = row["free_media_trial_cycle_end"]
        trial_cycle_end = (
            _iso(_parse_dt(trial_cycle_end_raw)) if trial_cycle_end_raw else ""
        )
        current_cycle_end = _iso(refill_at)
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
                refill_at,
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
        return self._recent_request_count(user_id=user_id)

    def _remember_recent_request(self, *, user_id: int, requested_at: datetime | None = None) -> None:
        now = requested_at or _utc_now()
        threshold = now - timedelta(minutes=1)
        key = str(int(user_id))
        history = self._recent_requests.setdefault(key, [])
        history.append(now)
        self._recent_requests[key] = [item for item in history if item >= threshold]

    def _recent_request_count(self, *, user_id: int) -> int:
        now = _utc_now()
        threshold = now - timedelta(minutes=1)
        key = str(int(user_id))
        history = self._recent_requests.get(key, [])
        if not isinstance(history, list):
            return 0
        filtered = [item for item in history if isinstance(item, datetime) and item >= threshold]
        self._recent_requests[key] = filtered
        return len(filtered)

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
            self._remember_recent_request(user_id=user_id)
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
                row = await connection.fetchrow(
                    "SELECT * FROM ai_users WHERE user_id = $1",
                    int(user_id),
                )
                if row is None:
                    raise RuntimeError("AI foydalanuvchi yozuvi yangilanmadi.")
                self._remember_recent_request(user_id=user_id)
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
        user = await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        now = _utc_now()
        balance = max(0, int(user.get("token_balance", 0) or 0))
        bonus_tokens = (
            int(credits)
            if isinstance(credits, int) and credits > 0
            else (premium_upgrade_tokens() if normalized_plan == "premium" else 0)
        )
        target_balance = balance + max(0, bonus_tokens)
        if normalized_plan == "free" and isinstance(credits, int) and credits >= 0:
            target_balance = int(credits)
        target_reset = _next_token_refill(now)

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
                        reset_date = $4,
                        free_media_trial_used = FALSE,
                        free_media_trial_cycle_end = $4,
                        lifetime_tokens_earned = lifetime_tokens_earned + $5,
                        updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    int(user_id),
                    normalized_plan,
                    int(target_balance),
                    target_reset,
                    max(0, bonus_tokens),
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
            record["free_reset_date"] = _iso(target_reset)
            record["reset_date"] = _iso(target_reset)
            record["free_media_trial_used"] = False
            record["free_media_trial_cycle_end"] = record["free_reset_date"]
            record["lifetime_tokens_earned"] = int(
                record.get("lifetime_tokens_earned", 0) or 0
            ) + max(0, bonus_tokens)
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
                    charged_amount = min(balance, token_amount)
                    if charged_amount <= 0:
                        return self._serialize_row(row)
                    await connection.execute(
                        """
                        UPDATE ai_users
                        SET token_balance = token_balance - $2,
                            lifetime_tokens_spent = lifetime_tokens_spent + $2,
                            updated_at = NOW()
                        WHERE user_id = $1
                        """,
                        int(user_id),
                        charged_amount,
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
            charged_amount = min(int(record.get("token_balance", 0) or 0), token_amount)
            if charged_amount <= 0:
                return json.loads(json.dumps(record))
            record["token_balance"] = int(record.get("token_balance", 0) or 0) - charged_amount
            record["lifetime_tokens_spent"] = int(
                record.get("lifetime_tokens_spent", 0) or 0
            ) + charged_amount
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
                    if _utc_now() > _parse_dt(referred["created_at"]) + timedelta(
                        minutes=_referral_claim_window_minutes()
                    ):
                        return {"applied": False, "reason": "expired"}
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
            if _utc_now() > _parse_dt(referred.get("created_at")) + timedelta(
                minutes=_referral_claim_window_minutes()
            ):
                return {"applied": False, "reason": "expired"}
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

    def _serialize_premium_request(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "request_id": int(request.get("request_id", 0) or 0),
            "user_id": int(request.get("user_id", 0) or 0),
            "username": str(request.get("username", "") or ""),
            "full_name": str(request.get("full_name", "") or ""),
            "status": str(request.get("status", "pending") or "pending"),
            "screenshot_file_id": str(request.get("screenshot_file_id", "") or ""),
            "screenshot_file_unique_id": str(
                request.get("screenshot_file_unique_id", "") or ""
            ),
            "screenshot_type": str(request.get("screenshot_type", "") or ""),
            "submitted_at": str(request.get("submitted_at", "") or ""),
            "reviewed_at": str(request.get("reviewed_at", "") or ""),
            "reviewed_by": int(request.get("reviewed_by", 0) or 0),
            "reviewer_note": str(request.get("reviewer_note", "") or ""),
            "admin_message_refs": list(request.get("admin_message_refs", []) or []),
        }

    def _serialize_premium_request_row(self, row: Any) -> dict[str, Any]:
        admin_message_refs = row["admin_message_refs"]
        if not isinstance(admin_message_refs, list):
            admin_message_refs = []
        return {
            "request_id": int(row["request_id"]),
            "user_id": int(row["user_id"]),
            "username": str(row["username"] or ""),
            "full_name": str(row["full_name"] or ""),
            "status": str(row["status"] or "pending"),
            "screenshot_file_id": str(row["screenshot_file_id"] or ""),
            "screenshot_file_unique_id": str(row["screenshot_file_unique_id"] or ""),
            "screenshot_type": str(row["screenshot_type"] or ""),
            "submitted_at": _iso(_parse_dt(row["submitted_at"])),
            "reviewed_at": (
                _iso(_parse_dt(row["reviewed_at"])) if row["reviewed_at"] else ""
            ),
            "reviewed_by": int(row["reviewed_by"] or 0),
            "reviewer_note": str(row["reviewer_note"] or ""),
            "admin_message_refs": admin_message_refs,
        }

    async def get_active_premium_request(
        self,
        *,
        user_id: int,
    ) -> dict[str, Any] | None:
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT *
                    FROM premium_requests
                    WHERE user_id = $1 AND status = 'pending'
                    ORDER BY submitted_at DESC
                    LIMIT 1
                    """,
                    int(user_id),
                )
            if row is None:
                return None
            return self._serialize_premium_request_row(row)

        await self._ensure_loaded()
        async with self._lock:
            requests = self._data.setdefault("premium_requests", {})
            candidates = [
                self._serialize_premium_request(item)
                for item in requests.values()
                if isinstance(item, dict)
                and int(item.get("user_id", 0) or 0) == int(user_id)
                and str(item.get("status", "") or "") == "pending"
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda item: str(item.get("submitted_at", "") or ""), reverse=True)
            return candidates[0]

    async def list_pending_premium_requests(
        self,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(50, int(limit)))
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT *
                    FROM premium_requests
                    WHERE status = 'pending'
                    ORDER BY submitted_at DESC
                    LIMIT $1
                    """,
                    safe_limit,
                )
            return [self._serialize_premium_request_row(row) for row in rows]

        await self._ensure_loaded()
        async with self._lock:
            requests = self._data.setdefault("premium_requests", {})
            candidates = [
                self._serialize_premium_request(item)
                for item in requests.values()
                if isinstance(item, dict)
                and str(item.get("status", "") or "") == "pending"
            ]
            candidates.sort(key=lambda item: str(item.get("submitted_at", "") or ""), reverse=True)
            return candidates[:safe_limit]

    async def create_premium_request(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        screenshot_file_id: str,
        screenshot_file_unique_id: str,
        screenshot_type: str,
    ) -> dict[str, Any]:
        clean_file_id = str(screenshot_file_id or "").strip()
        clean_unique_id = str(screenshot_file_unique_id or "").strip()
        clean_type = str(screenshot_type or "").strip().lower()
        if not clean_file_id or not clean_unique_id:
            raise ValueError("Screenshot topilmadi.")
        if clean_type not in {"photo", "document"}:
            raise ValueError("Screenshot turi noto'g'ri.")
        user = await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        if normalize_plan(str(user.get("current_plan", "free") or "free")) == "premium":
            raise ValueError("Premium allaqachon yoqilgan.")
        existing = await self.get_active_premium_request(user_id=user_id)
        if existing is not None:
            raise ValueError("Sizda allaqachon ko'rib chiqilayotgan so'rov bor.")

        if self._pool is not None:
            async with self._pool.acquire() as connection:
                try:
                    async with connection.transaction():
                        row = await connection.fetchrow(
                            """
                            INSERT INTO premium_requests (
                                user_id,
                                username,
                                full_name,
                                status,
                                screenshot_file_id,
                                screenshot_file_unique_id,
                                screenshot_type,
                                submitted_at
                            )
                            VALUES ($1, $2, $3, 'pending', $4, $5, $6, NOW())
                            RETURNING *
                            """,
                            int(user_id),
                            username,
                            full_name,
                            clean_file_id,
                            clean_unique_id,
                            clean_type,
                        )
                except Exception as error:
                    lowered = str(error or "").lower()
                    if "unique" in lowered or "idx_premium_requests_active_user" in lowered:
                        raise ValueError("Sizda allaqachon ko'rib chiqilayotgan so'rov bor.") from error
                    raise
                if row is None:
                    raise RuntimeError("Premium so'rovi saqlanmadi.")
                return self._serialize_premium_request_row(row)

        await self._ensure_loaded()
        async with self._lock:
            requests = self._data.setdefault("premium_requests", {})
            for item in requests.values():
                if not isinstance(item, dict):
                    continue
                if (
                    int(item.get("user_id", 0) or 0) == int(user_id)
                    and str(item.get("status", "") or "") == "pending"
                ):
                    raise ValueError("Sizda allaqachon ko'rib chiqilayotgan so'rov bor.")
            sequence = int(self._data.get("premium_request_sequence", 0) or 0) + 1
            self._data["premium_request_sequence"] = sequence
            now = _iso(_utc_now())
            record = {
                "request_id": sequence,
                "user_id": int(user_id),
                "username": username,
                "full_name": full_name,
                "status": "pending",
                "screenshot_file_id": clean_file_id,
                "screenshot_file_unique_id": clean_unique_id,
                "screenshot_type": clean_type,
                "submitted_at": now,
                "reviewed_at": "",
                "reviewed_by": 0,
                "reviewer_note": "",
                "admin_message_refs": [],
            }
            requests[str(sequence)] = record
            await self._save_locked()
            return self._serialize_premium_request(record)

    async def attach_premium_request_admin_message(
        self,
        *,
        request_id: int,
        chat_id: int,
        message_id: int,
    ) -> dict[str, Any] | None:
        ref = {"chat_id": int(chat_id), "message_id": int(message_id)}
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        "SELECT * FROM premium_requests WHERE request_id = $1 FOR UPDATE",
                        int(request_id),
                    )
                    if row is None:
                        return None
                    refs = row["admin_message_refs"]
                    if not isinstance(refs, list):
                        refs = []
                    if ref not in refs:
                        refs.append(ref)
                        await connection.execute(
                            """
                            UPDATE premium_requests
                            SET admin_message_refs = $2::jsonb
                            WHERE request_id = $1
                            """,
                            int(request_id),
                            json.dumps(refs, ensure_ascii=False),
                        )
                        row = await connection.fetchrow(
                            "SELECT * FROM premium_requests WHERE request_id = $1",
                            int(request_id),
                        )
                if row is None:
                    return None
                return self._serialize_premium_request_row(row)

        await self._ensure_loaded()
        async with self._lock:
            requests = self._data.setdefault("premium_requests", {})
            record = requests.get(str(int(request_id)))
            if not isinstance(record, dict):
                return None
            refs = list(record.get("admin_message_refs", []) or [])
            if ref not in refs:
                refs.append(ref)
                record["admin_message_refs"] = refs
                await self._save_locked()
            return self._serialize_premium_request(record)

    async def review_premium_request(
        self,
        *,
        request_id: int,
        reviewer_id: int,
        approve: bool,
        reviewer_note: str = "",
    ) -> dict[str, Any]:
        normalized_status = "approved" if approve else "rejected"
        note = str(reviewer_note or "").strip()
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    request_row = await connection.fetchrow(
                        "SELECT * FROM premium_requests WHERE request_id = $1 FOR UPDATE",
                        int(request_id),
                    )
                    if request_row is None:
                        return {"ok": False, "reason": "missing"}
                    if str(request_row["status"] or "") != "pending":
                        return {
                            "ok": False,
                            "reason": "processed",
                            "request": self._serialize_premium_request_row(request_row),
                        }
                    await connection.execute(
                        """
                        UPDATE premium_requests
                        SET status = $2,
                            reviewed_at = NOW(),
                            reviewed_by = $3,
                            reviewer_note = $4
                        WHERE request_id = $1
                        """,
                        int(request_id),
                        normalized_status,
                        int(reviewer_id),
                        note,
                    )
                    user_row = None
                    awarded_tokens = 0
                    if approve:
                        awarded_tokens = premium_upgrade_tokens()
                        next_refill = _next_token_refill()
                        await connection.execute(
                            """
                            UPDATE ai_users
                            SET current_plan = 'premium',
                                selected_plan = 'auto',
                                selected_model = 'auto',
                                token_balance = token_balance + $2,
                                free_requests_used = 0,
                                free_reset_date = $3,
                                reset_date = $3,
                                free_media_trial_used = FALSE,
                                free_media_trial_cycle_end = $3,
                                lifetime_tokens_earned = lifetime_tokens_earned + $2,
                                updated_at = NOW()
                            WHERE user_id = $1
                            """,
                            int(request_row["user_id"]),
                            int(awarded_tokens),
                            next_refill,
                        )
                    request_row = await connection.fetchrow(
                        "SELECT * FROM premium_requests WHERE request_id = $1",
                        int(request_id),
                    )
                    user_row = await connection.fetchrow(
                        "SELECT * FROM ai_users WHERE user_id = $1",
                        int(request_row["user_id"]),
                    )
                if request_row is None or user_row is None:
                    raise RuntimeError("Premium so'rovi ko'rib chiqilmadi.")
                return {
                    "ok": True,
                    "status": normalized_status,
                    "awarded_tokens": int(awarded_tokens),
                    "request": self._serialize_premium_request_row(request_row),
                    "user": self._serialize_row(user_row),
                }

        await self._ensure_loaded()
        async with self._lock:
            requests = self._data.setdefault("premium_requests", {})
            record = requests.get(str(int(request_id)))
            if not isinstance(record, dict):
                return {"ok": False, "reason": "missing"}
            if str(record.get("status", "") or "") != "pending":
                return {
                    "ok": False,
                    "reason": "processed",
                    "request": self._serialize_premium_request(record),
                }
            users = self._data.setdefault("users", {})
            user = users.get(str(int(record.get("user_id", 0) or 0)))
            if not isinstance(user, dict):
                return {"ok": False, "reason": "missing"}
            now = _utc_now()
            awarded_tokens = premium_upgrade_tokens() if approve else 0
            record["status"] = normalized_status
            record["reviewed_at"] = _iso(now)
            record["reviewed_by"] = int(reviewer_id)
            record["reviewer_note"] = note
            self._normalize_user_locked(user, username="", full_name="")
            if approve:
                next_refill = _next_token_refill(now)
                user["current_plan"] = "premium"
                user["selected_plan"] = MODEL_ALIAS_AUTO
                user["selected_model"] = MODEL_ALIAS_AUTO
                user["token_balance"] = int(user.get("token_balance", 0) or 0) + awarded_tokens
                user["free_requests_used"] = 0
                user["free_reset_date"] = _iso(next_refill)
                user["reset_date"] = _iso(next_refill)
                user["free_media_trial_used"] = False
                user["free_media_trial_cycle_end"] = _iso(next_refill)
                user["lifetime_tokens_earned"] = int(
                    user.get("lifetime_tokens_earned", 0) or 0
                ) + awarded_tokens
                user["updated_at"] = _iso(now)
            await self._save_locked()
            return {
                "ok": True,
                "status": normalized_status,
                "awarded_tokens": int(awarded_tokens),
                "request": self._serialize_premium_request(record),
                "user": json.loads(json.dumps(user)),
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
