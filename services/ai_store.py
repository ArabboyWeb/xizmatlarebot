from __future__ import annotations

import asyncio
import calendar
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware

from services.ai_costs import premium_safe_ai_budget_usd
from services.ai_gateway import MODEL_ALIAS_AUTO, clamp_selected_plan, effective_selected_plan
from services.token_pricing import (
    free_ai_chat_cooldown_seconds,
    free_ai_chat_limit_per_day,
    free_ai_image_cooldown_seconds,
    free_ai_image_limit_per_day,
    free_daily_tokens,
    free_signup_tokens,
    normalize_plan,
    premium_ai_image_cooldown_seconds,
    premium_daily_credit_cap,
    premium_daily_tokens,
    premium_monthly_credits,
    premium_upgrade_tokens,
    referral_invitee_bonus,
    referral_inviter_bonus,
    refill_interval_hours,
    resolve_service_key,
    service_daily_limit,
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


try:
    BOT_TIMEZONE = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Tashkent"))
except Exception:  # pragma: no cover
    BOT_TIMEZONE = timezone.utc
HOLD_TTL_SECONDS = max(30, _read_int("AI_CREDIT_HOLD_TTL_SECONDS", 300))
TRANSACTION_LOG_LIMIT = max(10, _read_int("AI_TRANSACTION_LOG_LIMIT", 100))


def _add_months(value: datetime, months: int = 1) -> datetime:
    normalized = value.astimezone(timezone.utc)
    month_index = normalized.month - 1 + months
    year = normalized.year + month_index // 12
    month = month_index % 12 + 1
    day = min(normalized.day, calendar.monthrange(year, month)[1])
    return normalized.replace(year=year, month=month, day=day)


def _today_key(now: datetime | None = None) -> str:
    target = (now or _utc_now()).astimezone(BOT_TIMEZONE)
    return target.date().isoformat()


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
            "credit_holds": {},
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
            credit_balance BIGINT NOT NULL DEFAULT 100,
            monthly_credits BIGINT NOT NULL DEFAULT 0,
            credits_spent BIGINT NOT NULL DEFAULT 0,
            credits_added BIGINT NOT NULL DEFAULT 0,
            last_credit_reset TIMESTAMPTZ,
            premium_started_at TIMESTAMPTZ,
            next_credit_reset_at TIMESTAMPTZ,
            daily_credit_cap BIGINT NOT NULL DEFAULT 0,
            daily_credits_used BIGINT NOT NULL DEFAULT 0,
            daily_credits_used_date TEXT NOT NULL DEFAULT '',
            ai_budget_cap_usd DOUBLE PRECISION NOT NULL DEFAULT 0.70,
            ai_budget_spent_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
            usage_counters JSONB NOT NULL DEFAULT '{}'::jsonb,
            transaction_log JSONB NOT NULL DEFAULT '[]'::jsonb,
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
        CREATE TABLE IF NOT EXISTS credit_holds (
            hold_id TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            service_key TEXT NOT NULL,
            credits BIGINT NOT NULL DEFAULT 0,
            estimated_ai_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'held',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX IF NOT EXISTS idx_credit_holds_user_status
            ON credit_holds (user_id, status, expires_at DESC);
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
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS credit_balance BIGINT NOT NULL DEFAULT 100;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS monthly_credits BIGINT NOT NULL DEFAULT 0;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS credits_spent BIGINT NOT NULL DEFAULT 0;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS credits_added BIGINT NOT NULL DEFAULT 0;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS last_credit_reset TIMESTAMPTZ;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS premium_started_at TIMESTAMPTZ;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS next_credit_reset_at TIMESTAMPTZ;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS daily_credit_cap BIGINT NOT NULL DEFAULT 0;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS daily_credits_used BIGINT NOT NULL DEFAULT 0;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS daily_credits_used_date TEXT NOT NULL DEFAULT '';"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS ai_budget_cap_usd DOUBLE PRECISION NOT NULL DEFAULT 0.70;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS ai_budget_spent_usd DOUBLE PRECISION NOT NULL DEFAULT 0;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS usage_counters JSONB NOT NULL DEFAULT '{}'::jsonb;"
            )
            await connection.execute(
                "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS transaction_log JSONB NOT NULL DEFAULT '[]'::jsonb;"
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
            self._data.setdefault("credit_holds", {})
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
            "credit_balance": signup_tokens,
            "token_balance": signup_tokens,
            "monthly_credits": 0,
            "credits_spent": 0,
            "credits_added": signup_tokens,
            "last_credit_reset": "",
            "premium_started_at": "",
            "next_credit_reset_at": "",
            "daily_credit_cap": premium_daily_credit_cap(),
            "daily_credits_used": 0,
            "daily_credits_used_date": _today_key(now),
            "ai_budget_cap_usd": premium_safe_ai_budget_usd(),
            "ai_budget_spent_usd": 0.0,
            "usage_counters": {},
            "transaction_log": [],
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

    def _append_transaction_log_locked(
        self,
        user: dict[str, Any],
        *,
        tx_type: str,
        service_key: str,
        amount: int,
        balance_after: int,
        status: str = "ok",
        note: str = "",
        estimated_ai_cost_usd: float = 0.0,
    ) -> None:
        history = user.setdefault("transaction_log", [])
        if not isinstance(history, list):
            history = []
            user["transaction_log"] = history
        history.append(
            {
                "id": uuid4().hex,
                "timestamp": _iso(_utc_now()),
                "type": str(tx_type or "").strip().lower(),
                "service_key": resolve_service_key(service_key),
                "amount": max(0, int(amount)),
                "balance_after": max(0, int(balance_after)),
                "status": str(status or "ok").strip().lower(),
                "estimated_ai_cost_usd": round(max(0.0, float(estimated_ai_cost_usd)), 6),
                "note": str(note or "").strip(),
            }
        )
        del history[:-TRANSACTION_LOG_LIMIT]

    def _usage_entry_locked(
        self,
        user: dict[str, Any],
        *,
        service_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        usage = user.setdefault("usage_counters", {})
        if not isinstance(usage, dict):
            usage = {}
            user["usage_counters"] = usage
        key = resolve_service_key(service_key)
        entry = usage.get(key)
        if not isinstance(entry, dict):
            entry = {"day": _today_key(now), "count": 0, "last_used_at": ""}
            usage[key] = entry
        current_day = _today_key(now)
        if str(entry.get("day", "") or "") != current_day:
            entry["day"] = current_day
            entry["count"] = 0
        entry["last_used_at"] = str(entry.get("last_used_at", "") or "")
        return entry

    def _active_holds_locked(
        self,
        *,
        user_id: int,
        now: datetime | None = None,
    ) -> tuple[int, float]:
        current = now or _utc_now()
        holds = self._data.setdefault("credit_holds", {})
        reserved_credits = 0
        reserved_cost = 0.0
        for hold_id, record in list(holds.items()):
            if not isinstance(record, dict):
                holds.pop(hold_id, None)
                continue
            if int(record.get("user_id", 0) or 0) != int(user_id):
                continue
            expires_at = _parse_dt(record.get("expires_at"))
            status = str(record.get("status", "held") or "held").strip().lower()
            if status != "held" or expires_at <= current:
                holds.pop(hold_id, None)
                continue
            reserved_credits += max(0, int(record.get("credits", 0) or 0))
            reserved_cost += max(0.0, float(record.get("estimated_ai_cost_usd", 0.0) or 0.0))
        return reserved_credits, round(reserved_cost, 6)

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
        credit_balance = max(
            0,
            int(
                user.get(
                    "credit_balance",
                    user.get("token_balance", 0),
                )
                or 0
            ),
        )
        credits_added = max(
            0,
            int(
                user.get(
                    "credits_added",
                    user.get("lifetime_tokens_earned", credit_balance),
                )
                or 0
            ),
        )
        credits_spent = max(
            0,
            int(
                user.get(
                    "credits_spent",
                    user.get("lifetime_tokens_spent", 0),
                )
                or 0
            ),
        )
        monthly_credits = (
            premium_monthly_credits()
            if current_plan == "premium"
            else max(0, int(user.get("monthly_credits", 0) or 0))
        )
        daily_credit_cap = max(
            0,
            int(
                user.get(
                    "daily_credit_cap",
                    premium_daily_credit_cap(),
                )
                or 0
            ),
        )
        ai_budget_cap_usd = round(
            max(
                0.01,
                float(user.get("ai_budget_cap_usd", premium_safe_ai_budget_usd()) or 0.70),
            ),
            6,
        )
        ai_budget_spent_usd = round(
            max(0.0, float(user.get("ai_budget_spent_usd", 0.0) or 0.0)),
            6,
        )
        today_key = _today_key(now)
        daily_credits_used_date = str(user.get("daily_credits_used_date", "") or "")
        daily_credits_used = max(0, int(user.get("daily_credits_used", 0) or 0))
        if daily_credits_used_date != today_key:
            daily_credits_used = 0
            daily_credits_used_date = today_key

        refill_default = _next_token_refill(now)
        free_reset_raw = user.get("free_reset_date", "") or user.get("reset_date", "")
        free_refill_at = (
            _parse_dt(free_reset_raw)
            if str(free_reset_raw or "").strip()
            else refill_default
        )
        if current_plan == "free" and now >= free_refill_at:
            refill_amount = _refill_amount("free")
            credit_balance += refill_amount
            credits_added += refill_amount
            user["free_requests_used"] = 0
            free_refill_at = _next_token_refill(now)
        user["free_reset_date"] = _iso(free_refill_at)

        premium_started_at = str(user.get("premium_started_at", "") or "").strip()
        last_credit_reset = str(user.get("last_credit_reset", "") or "").strip()
        next_credit_reset_at = str(user.get("next_credit_reset_at", "") or "").strip()
        if current_plan == "premium":
            if not premium_started_at or not next_credit_reset_at:
                premium_started = now
                last_reset = now
                next_reset = _add_months(now, 1)
                credit_balance = monthly_credits
                credits_added = max(credits_added, 0) + monthly_credits
                ai_budget_spent_usd = 0.0
            else:
                premium_started = _parse_dt(premium_started_at)
                last_reset = _parse_dt(last_credit_reset) if last_credit_reset else premium_started
                next_reset = _parse_dt(next_credit_reset_at)
                if now >= next_reset:
                    while now >= next_reset:
                        last_reset = next_reset
                        next_reset = _add_months(next_reset, 1)
                    credit_balance = monthly_credits
                    credits_added += monthly_credits
                    ai_budget_spent_usd = 0.0
                    daily_credits_used = 0
                    daily_credits_used_date = today_key
            user["premium_started_at"] = _iso(premium_started)
            user["last_credit_reset"] = _iso(last_reset)
            user["next_credit_reset_at"] = _iso(next_reset)
            user["reset_date"] = _iso(next_reset)
        else:
            user["premium_started_at"] = premium_started_at
            user["last_credit_reset"] = last_credit_reset
            user["next_credit_reset_at"] = ""
            user["reset_date"] = user["free_reset_date"]
            monthly_credits = 0

        user["credit_balance"] = max(0, int(credit_balance))
        user["token_balance"] = int(user["credit_balance"])
        user["monthly_credits"] = max(0, int(monthly_credits))
        user["credits_added"] = max(credits_added, int(user["credit_balance"]))
        user["credits_spent"] = credits_spent
        user["daily_credit_cap"] = daily_credit_cap
        user["daily_credits_used"] = daily_credits_used
        user["daily_credits_used_date"] = daily_credits_used_date
        user["ai_budget_cap_usd"] = ai_budget_cap_usd
        user["ai_budget_spent_usd"] = ai_budget_spent_usd
        if not isinstance(user.get("usage_counters"), dict):
            user["usage_counters"] = {}
        if not isinstance(user.get("transaction_log"), list):
            user["transaction_log"] = []
        del user["transaction_log"][:-TRANSACTION_LOG_LIMIT]

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
            int(user.get("credits_added", 0) or 0),
            int(user.get("lifetime_tokens_earned", 0) or 0),
        )
        user["lifetime_tokens_spent"] = max(
            int(user.get("credits_spent", 0) or 0),
            int(user.get("lifetime_tokens_spent", 0) or 0),
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
                        credit_balance,
                        monthly_credits,
                        credits_added,
                        free_requests_used,
                        free_reset_date,
                        reset_date,
                        daily_credit_cap,
                        daily_credits_used_date,
                        ai_budget_cap_usd,
                        lifetime_tokens_earned,
                        created_at,
                        updated_at
                    )
                    VALUES ($1, $2, $3, 'free', $4, $4, 0, $4, 0, $5, $5, $6, $7, $8, $4, NOW(), NOW())
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
                    premium_daily_credit_cap(),
                    _today_key(),
                    premium_safe_ai_budget_usd(),
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
        current = self._serialize_row(row)
        original = json.loads(json.dumps(current))
        normalized = self._normalize_user_locked(
            current,
            username=username,
            full_name=full_name,
        )
        if normalized != original:
            await self._db_write_user_locked(connection, normalized)
            final_row = await connection.fetchrow(
                "SELECT * FROM ai_users WHERE user_id = $1",
                int(row["user_id"]),
            )
            if final_row is None:
                raise RuntimeError("AI foydalanuvchi yozuvi topilmadi.")
            return self._serialize_row(final_row)
        return normalized

    async def _db_write_user_locked(self, connection: Any, user: dict[str, Any]) -> None:
        await connection.execute(
            """
            UPDATE ai_users
            SET username = $2,
                full_name = $3,
                current_plan = $4,
                selected_plan = $5,
                selected_model = $6,
                token_balance = $7,
                credit_balance = $8,
                monthly_credits = $9,
                credits_spent = $10,
                credits_added = $11,
                last_credit_reset = $12,
                premium_started_at = $13,
                next_credit_reset_at = $14,
                daily_credit_cap = $15,
                daily_credits_used = $16,
                daily_credits_used_date = $17,
                ai_budget_cap_usd = $18,
                ai_budget_spent_usd = $19,
                usage_counters = $20::jsonb,
                transaction_log = $21::jsonb,
                free_requests_used = $22,
                free_reset_date = $23,
                reset_date = $24,
                last_request_at = $25,
                total_prompt_tokens = $26,
                total_completion_tokens = $27,
                referrer_id = $28,
                referral_count = $29,
                referral_bonus_claimed = $30,
                free_media_trial_used = $31,
                free_media_trial_cycle_end = $32,
                lifetime_tokens_earned = $33,
                lifetime_tokens_spent = $34,
                updated_at = NOW()
            WHERE user_id = $1
            """,
            int(user["user_id"]),
            str(user.get("username", "") or ""),
            str(user.get("full_name", "") or ""),
            str(user.get("current_plan", "free") or "free"),
            str(user.get("selected_plan", MODEL_ALIAS_AUTO) or MODEL_ALIAS_AUTO),
            str(user.get("selected_model", MODEL_ALIAS_AUTO) or MODEL_ALIAS_AUTO),
            int(user.get("token_balance", 0) or 0),
            int(user.get("credit_balance", 0) or 0),
            int(user.get("monthly_credits", 0) or 0),
            int(user.get("credits_spent", 0) or 0),
            int(user.get("credits_added", 0) or 0),
            (
                _parse_dt(user.get("last_credit_reset"))
                if str(user.get("last_credit_reset", "") or "").strip()
                else None
            ),
            (
                _parse_dt(user.get("premium_started_at"))
                if str(user.get("premium_started_at", "") or "").strip()
                else None
            ),
            (
                _parse_dt(user.get("next_credit_reset_at"))
                if str(user.get("next_credit_reset_at", "") or "").strip()
                else None
            ),
            int(user.get("daily_credit_cap", 0) or 0),
            int(user.get("daily_credits_used", 0) or 0),
            str(user.get("daily_credits_used_date", "") or ""),
            float(user.get("ai_budget_cap_usd", premium_safe_ai_budget_usd()) or 0.70),
            float(user.get("ai_budget_spent_usd", 0.0) or 0.0),
            json.dumps(user.get("usage_counters", {}), ensure_ascii=False),
            json.dumps(user.get("transaction_log", []), ensure_ascii=False),
            int(user.get("free_requests_used", 0) or 0),
            _parse_dt(user.get("free_reset_date")),
            _parse_dt(user.get("reset_date")),
            (
                _parse_dt(user.get("last_request_at"))
                if str(user.get("last_request_at", "") or "").strip()
                else None
            ),
            int(user.get("total_prompt_tokens", 0) or 0),
            int(user.get("total_completion_tokens", 0) or 0),
            int(user.get("referrer_id", 0) or 0) or None,
            int(user.get("referral_count", 0) or 0),
            bool(user.get("referral_bonus_claimed", False)),
            bool(user.get("free_media_trial_used", False)),
            _parse_dt(user.get("free_media_trial_cycle_end")),
            int(user.get("lifetime_tokens_earned", 0) or 0),
            int(user.get("lifetime_tokens_spent", 0) or 0),
        )

    def _serialize_row(self, row: Any) -> dict[str, Any]:
        usage_counters = row["usage_counters"] if "usage_counters" in row else {}
        if not isinstance(usage_counters, dict):
            usage_counters = {}
        transaction_log = row["transaction_log"] if "transaction_log" in row else []
        if not isinstance(transaction_log, list):
            transaction_log = []
        return {
            "user_id": int(row["user_id"]),
            "username": str(row["username"] or ""),
            "full_name": str(row["full_name"] or ""),
            "current_plan": normalize_plan(str(row["current_plan"] or "free")),
            "selected_plan": str(row["selected_plan"] or MODEL_ALIAS_AUTO),
            "selected_model": str(row["selected_model"] or MODEL_ALIAS_AUTO),
            "credit_balance": int(row["credit_balance"] or row["token_balance"] or 0),
            "token_balance": int(row["token_balance"] or 0),
            "monthly_credits": int(row["monthly_credits"] or 0),
            "credits_spent": int(row["credits_spent"] or row["lifetime_tokens_spent"] or 0),
            "credits_added": int(row["credits_added"] or row["lifetime_tokens_earned"] or 0),
            "last_credit_reset": (
                _iso(_parse_dt(row["last_credit_reset"]))
                if row["last_credit_reset"]
                else ""
            ),
            "premium_started_at": (
                _iso(_parse_dt(row["premium_started_at"]))
                if row["premium_started_at"]
                else ""
            ),
            "next_credit_reset_at": (
                _iso(_parse_dt(row["next_credit_reset_at"]))
                if row["next_credit_reset_at"]
                else ""
            ),
            "daily_credit_cap": int(row["daily_credit_cap"] or 0),
            "daily_credits_used": int(row["daily_credits_used"] or 0),
            "daily_credits_used_date": str(row["daily_credits_used_date"] or ""),
            "ai_budget_cap_usd": float(row["ai_budget_cap_usd"] or premium_safe_ai_budget_usd()),
            "ai_budget_spent_usd": float(row["ai_budget_spent_usd"] or 0.0),
            "usage_counters": usage_counters,
            "transaction_log": transaction_log,
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

    async def can_use_service_quota(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        service_key: str,
    ) -> dict[str, Any]:
        user = await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        limit = service_daily_limit(
            service_key,
            plan=str(user.get("current_plan", "free") or "free"),
        )
        if limit is None:
            return {"tracked": False, "allowed": True, "limit": None, "used": 0}
        if limit <= 0:
            return {"tracked": True, "allowed": True, "limit": 0, "used": 0}
        usage = user.get("usage_counters", {})
        if not isinstance(usage, dict):
            usage = {}
        entry = usage.get(resolve_service_key(service_key), {})
        if not isinstance(entry, dict) or str(entry.get("day", "") or "") != _today_key():
            used = 0
        else:
            used = max(0, int(entry.get("count", 0) or 0))
        return {
            "tracked": True,
            "allowed": used < limit,
            "limit": limit,
            "used": used,
            "remaining": max(0, limit - used),
        }

    async def consume_service_quota(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        service_key: str,
    ) -> dict[str, Any]:
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        "SELECT * FROM ai_users WHERE user_id = $1 FOR UPDATE",
                        int(user_id),
                    )
                    if row is None:
                        await self._db_ensure_user(
                            user_id=user_id,
                            username=username,
                            full_name=full_name,
                        )
                        row = await connection.fetchrow(
                            "SELECT * FROM ai_users WHERE user_id = $1 FOR UPDATE",
                            int(user_id),
                        )
                    if row is None:
                        raise RuntimeError("AI foydalanuvchi topilmadi.")
                    user = self._normalize_user_locked(
                        self._serialize_row(row),
                        username=username,
                        full_name=full_name,
                    )
                    limit = service_daily_limit(
                        service_key,
                        plan=str(user.get("current_plan", "free") or "free"),
                    )
                    if limit is not None and limit > 0:
                        entry = self._usage_entry_locked(user, service_key=service_key)
                        if int(entry.get("count", 0) or 0) >= limit:
                            return user
                        entry["count"] = int(entry.get("count", 0) or 0) + 1
                        entry["last_used_at"] = _iso(_utc_now())
                        user["last_request_at"] = entry["last_used_at"]
                        await self._db_write_user_locked(connection, user)
                    return user

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
            user = self._normalize_user_locked(record, username=username, full_name=full_name)
            limit = service_daily_limit(
                service_key,
                plan=str(user.get("current_plan", "free") or "free"),
            )
            if limit is not None and limit > 0:
                entry = self._usage_entry_locked(user, service_key=service_key)
                if int(entry.get("count", 0) or 0) < limit:
                    entry["count"] = int(entry.get("count", 0) or 0) + 1
                    entry["last_used_at"] = _iso(_utc_now())
                    user["last_request_at"] = entry["last_used_at"]
                    await self._save_locked()
            return json.loads(json.dumps(user))

    async def authorize_ai_service(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        service_key: str,
        credit_cost: int,
        estimated_ai_cost_usd: float,
        cooldown_seconds: int = 0,
        free_daily_limit: int | None = None,
        premium_only: bool = False,
    ) -> dict[str, Any]:
        if self._pool is not None:
            return await self._db_authorize_ai_service(
                user_id=user_id,
                username=username,
                full_name=full_name,
                service_key=service_key,
                credit_cost=credit_cost,
                estimated_ai_cost_usd=estimated_ai_cost_usd,
                cooldown_seconds=cooldown_seconds,
                free_daily_limit=free_daily_limit,
                premium_only=premium_only,
            )

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            holds = self._data.setdefault("credit_holds", {})
            key = str(int(user_id))
            user = users.get(key)
            if not isinstance(user, dict):
                user = self._default_user(
                    user_id=user_id,
                    username=username,
                    full_name=full_name,
                )
                users[key] = user
            user = self._normalize_user_locked(user, username=username, full_name=full_name)
            now = _utc_now()
            plan = str(user.get("current_plan", "free") or "free")
            if premium_only and plan != "premium":
                return {"ok": False, "reason": "premium_only", "user": json.loads(json.dumps(user))}

            rpm = _plan_rpm(plan)
            if self._recent_request_count(user_id=user_id) >= rpm:
                return {"ok": False, "reason": "rpm_limit", "wait_seconds": 60, "user": json.loads(json.dumps(user))}

            usage_entry = self._usage_entry_locked(user, service_key=service_key, now=now)
            last_used_at = str(usage_entry.get("last_used_at", "") or "")
            if cooldown_seconds > 0 and last_used_at:
                elapsed = (now - _parse_dt(last_used_at)).total_seconds()
                if elapsed < cooldown_seconds:
                    return {
                        "ok": False,
                        "reason": "cooldown",
                        "wait_seconds": int(cooldown_seconds - elapsed + 0.999),
                        "user": json.loads(json.dumps(user)),
                    }

            if plan == "free":
                if isinstance(free_daily_limit, int) and free_daily_limit > 0:
                    if int(usage_entry.get("count", 0) or 0) >= free_daily_limit:
                        return {
                            "ok": False,
                            "reason": "daily_limit",
                            "limit": free_daily_limit,
                            "used": int(usage_entry.get("count", 0) or 0),
                            "user": json.loads(json.dumps(user)),
                        }
                self._remember_recent_request(user_id=user_id)
                return {
                    "ok": True,
                    "plan": "free",
                    "hold_id": "",
                    "credit_cost": 0,
                    "estimated_ai_cost_usd": 0.0,
                    "user": json.loads(json.dumps(user)),
                }

            reserved_credits, reserved_cost = self._active_holds_locked(user_id=user_id, now=now)
            available_credits = max(0, int(user.get("credit_balance", 0) or 0) - reserved_credits)
            if available_credits < max(0, int(credit_cost)):
                return {
                    "ok": False,
                    "reason": "insufficient_credits",
                    "required": max(0, int(credit_cost)),
                    "available": available_credits,
                    "user": json.loads(json.dumps(user)),
                }
            daily_cap = max(0, int(user.get("daily_credit_cap", 0) or 0))
            daily_used = max(0, int(user.get("daily_credits_used", 0) or 0))
            if daily_cap > 0 and daily_used + reserved_credits + int(credit_cost) > daily_cap:
                return {
                    "ok": False,
                    "reason": "daily_credit_cap",
                    "limit": daily_cap,
                    "used": daily_used,
                    "user": json.loads(json.dumps(user)),
                }
            budget_cap = float(user.get("ai_budget_cap_usd", premium_safe_ai_budget_usd()) or 0.70)
            budget_spent = float(user.get("ai_budget_spent_usd", 0.0) or 0.0)
            if budget_spent + reserved_cost + float(estimated_ai_cost_usd) > budget_cap + 1e-9:
                return {
                    "ok": False,
                    "reason": "budget_cap",
                    "budget_cap_usd": round(budget_cap, 6),
                    "budget_spent_usd": round(budget_spent, 6),
                    "user": json.loads(json.dumps(user)),
                }
            hold_id = uuid4().hex
            holds[hold_id] = {
                "hold_id": hold_id,
                "user_id": int(user_id),
                "service_key": resolve_service_key(service_key),
                "credits": max(0, int(credit_cost)),
                "estimated_ai_cost_usd": round(max(0.0, float(estimated_ai_cost_usd)), 6),
                "status": "held",
                "created_at": _iso(now),
                "expires_at": _iso(now + timedelta(seconds=HOLD_TTL_SECONDS)),
            }
            self._remember_recent_request(user_id=user_id)
            await self._save_locked()
            return {
                "ok": True,
                "plan": "premium",
                "hold_id": hold_id,
                "credit_cost": max(0, int(credit_cost)),
                "estimated_ai_cost_usd": round(max(0.0, float(estimated_ai_cost_usd)), 6),
                "user": json.loads(json.dumps(user)),
            }

    async def finalize_ai_service(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        service_key: str,
        ok: bool,
        hold_id: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        actual_ai_cost_usd: float | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        if self._pool is not None:
            return await self._db_finalize_ai_service(
                user_id=user_id,
                username=username,
                full_name=full_name,
                service_key=service_key,
                ok=ok,
                hold_id=hold_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                actual_ai_cost_usd=actual_ai_cost_usd,
                note=note,
            )

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            holds = self._data.setdefault("credit_holds", {})
            record = users.get(str(int(user_id)))
            if not isinstance(record, dict):
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            user = self._normalize_user_locked(record, username=username, full_name=full_name)
            now = _utc_now()

            if ok:
                usage_entry = self._usage_entry_locked(user, service_key=service_key, now=now)
                usage_entry["count"] = int(usage_entry.get("count", 0) or 0) + 1
                usage_entry["last_used_at"] = _iso(now)
                user["last_request_at"] = usage_entry["last_used_at"]
                user["total_prompt_tokens"] = int(user.get("total_prompt_tokens", 0) or 0) + int(prompt_tokens)
                user["total_completion_tokens"] = int(user.get("total_completion_tokens", 0) or 0) + int(completion_tokens)

                if hold_id:
                    hold = holds.pop(str(hold_id), None)
                    if isinstance(hold, dict):
                        amount = max(0, int(hold.get("credits", 0) or 0))
                        estimated_cost = round(
                            max(
                                0.0,
                                float(
                                    actual_ai_cost_usd
                                    if actual_ai_cost_usd is not None
                                    else hold.get("estimated_ai_cost_usd", 0.0)
                                )
                                or 0.0
                            ),
                            6,
                        )
                        today_key = _today_key(now)
                        if str(user.get("daily_credits_used_date", "") or "") != today_key:
                            user["daily_credits_used"] = 0
                            user["daily_credits_used_date"] = today_key
                        user["credit_balance"] = max(0, int(user.get("credit_balance", 0) or 0) - amount)
                        user["token_balance"] = int(user["credit_balance"])
                        user["credits_spent"] = int(user.get("credits_spent", 0) or 0) + amount
                        user["lifetime_tokens_spent"] = int(user.get("credits_spent", 0) or 0)
                        user["daily_credits_used"] = int(user.get("daily_credits_used", 0) or 0) + amount
                        user["ai_budget_spent_usd"] = round(
                            float(user.get("ai_budget_spent_usd", 0.0) or 0.0) + estimated_cost,
                            6,
                        )
                        self._append_transaction_log_locked(
                            user,
                            tx_type="debit",
                            service_key=service_key,
                            amount=amount,
                            balance_after=int(user.get("credit_balance", 0) or 0),
                            estimated_ai_cost_usd=estimated_cost,
                            note=note,
                        )
                user["updated_at"] = _iso(now)
            else:
                if hold_id:
                    holds.pop(str(hold_id), None)
                user["updated_at"] = _iso(now)
            await self._save_locked()
            return json.loads(json.dumps(user))

    async def _db_authorize_ai_service(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        service_key: str,
        credit_cost: int,
        estimated_ai_cost_usd: float,
        cooldown_seconds: int = 0,
        free_daily_limit: int | None = None,
        premium_only: bool = False,
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
                row = await connection.fetchrow(
                    "SELECT * FROM ai_users WHERE user_id = $1 FOR UPDATE",
                    int(user_id),
                )
                if row is None:
                    raise RuntimeError("AI foydalanuvchi topilmadi.")
                user = self._normalize_user_locked(
                    self._serialize_row(row),
                    username=username,
                    full_name=full_name,
                )
                await self._db_write_user_locked(connection, user)
                if premium_only and str(user.get("current_plan", "free") or "free") != "premium":
                    return {"ok": False, "reason": "premium_only", "user": user}

                rpm = _plan_rpm(str(user.get("current_plan", "free") or "free"))
                if self._recent_request_count(user_id=user_id) >= rpm:
                    return {"ok": False, "reason": "rpm_limit", "wait_seconds": 60, "user": user}

                usage_entry = self._usage_entry_locked(user, service_key=service_key)
                last_used_at = str(usage_entry.get("last_used_at", "") or "")
                now = _utc_now()
                if cooldown_seconds > 0 and last_used_at:
                    elapsed = (now - _parse_dt(last_used_at)).total_seconds()
                    if elapsed < cooldown_seconds:
                        return {
                            "ok": False,
                            "reason": "cooldown",
                            "wait_seconds": int(cooldown_seconds - elapsed + 0.999),
                            "user": user,
                        }

                if str(user.get("current_plan", "free") or "free") == "free":
                    if isinstance(free_daily_limit, int) and free_daily_limit > 0:
                        if int(usage_entry.get("count", 0) or 0) >= free_daily_limit:
                            return {
                                "ok": False,
                                "reason": "daily_limit",
                                "limit": free_daily_limit,
                                "used": int(usage_entry.get("count", 0) or 0),
                                "user": user,
                            }
                    self._remember_recent_request(user_id=user_id)
                    return {
                        "ok": True,
                        "plan": "free",
                        "hold_id": "",
                        "credit_cost": 0,
                        "estimated_ai_cost_usd": 0.0,
                        "user": user,
                    }

                await connection.execute(
                    """
                    DELETE FROM credit_holds
                    WHERE user_id = $1 AND status = 'held' AND expires_at <= NOW()
                    """,
                    int(user_id),
                )
                reserved = await connection.fetchrow(
                    """
                    SELECT COALESCE(SUM(credits), 0) AS reserved_credits,
                           COALESCE(SUM(estimated_ai_cost_usd), 0) AS reserved_cost
                    FROM credit_holds
                    WHERE user_id = $1 AND status = 'held'
                    """,
                    int(user_id),
                )
                reserved_credits = int(reserved["reserved_credits"] or 0) if reserved is not None else 0
                reserved_cost = float(reserved["reserved_cost"] or 0.0) if reserved is not None else 0.0
                available_credits = max(0, int(user.get("credit_balance", 0) or 0) - reserved_credits)
                if available_credits < max(0, int(credit_cost)):
                    return {
                        "ok": False,
                        "reason": "insufficient_credits",
                        "required": max(0, int(credit_cost)),
                        "available": available_credits,
                        "user": user,
                    }
                daily_cap = max(0, int(user.get("daily_credit_cap", 0) or 0))
                daily_used = max(0, int(user.get("daily_credits_used", 0) or 0))
                if daily_cap > 0 and daily_used + reserved_credits + int(credit_cost) > daily_cap:
                    return {
                        "ok": False,
                        "reason": "daily_credit_cap",
                        "limit": daily_cap,
                        "used": daily_used,
                        "user": user,
                    }
                budget_cap = float(user.get("ai_budget_cap_usd", premium_safe_ai_budget_usd()) or 0.70)
                budget_spent = float(user.get("ai_budget_spent_usd", 0.0) or 0.0)
                if budget_spent + reserved_cost + float(estimated_ai_cost_usd) > budget_cap + 1e-9:
                    return {
                        "ok": False,
                        "reason": "budget_cap",
                        "budget_cap_usd": round(budget_cap, 6),
                        "budget_spent_usd": round(budget_spent, 6),
                        "user": user,
                    }
                hold_id = uuid4().hex
                await connection.execute(
                    """
                    INSERT INTO credit_holds (
                        hold_id,
                        user_id,
                        service_key,
                        credits,
                        estimated_ai_cost_usd,
                        status,
                        created_at,
                        expires_at,
                        metadata
                    )
                    VALUES ($1, $2, $3, $4, $5, 'held', NOW(), $6, '{}'::jsonb)
                    """,
                    hold_id,
                    int(user_id),
                    resolve_service_key(service_key),
                    max(0, int(credit_cost)),
                    round(max(0.0, float(estimated_ai_cost_usd)), 6),
                    _utc_now() + timedelta(seconds=HOLD_TTL_SECONDS),
                )
                self._remember_recent_request(user_id=user_id)
                return {
                    "ok": True,
                    "plan": "premium",
                    "hold_id": hold_id,
                    "credit_cost": max(0, int(credit_cost)),
                    "estimated_ai_cost_usd": round(max(0.0, float(estimated_ai_cost_usd)), 6),
                    "user": user,
                }

    async def _db_finalize_ai_service(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        service_key: str,
        ok: bool,
        hold_id: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        actual_ai_cost_usd: float | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        if self._pool is None:
            raise RuntimeError("AI database pool topilmadi.")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT * FROM ai_users WHERE user_id = $1 FOR UPDATE",
                    int(user_id),
                )
                if row is None:
                    raise RuntimeError("AI foydalanuvchi topilmadi.")
                user = self._normalize_user_locked(
                    self._serialize_row(row),
                    username=username,
                    full_name=full_name,
                )
                now = _utc_now()
                if ok:
                    usage_entry = self._usage_entry_locked(user, service_key=service_key, now=now)
                    usage_entry["count"] = int(usage_entry.get("count", 0) or 0) + 1
                    usage_entry["last_used_at"] = _iso(now)
                    user["last_request_at"] = usage_entry["last_used_at"]
                    user["total_prompt_tokens"] = int(user.get("total_prompt_tokens", 0) or 0) + int(prompt_tokens)
                    user["total_completion_tokens"] = int(user.get("total_completion_tokens", 0) or 0) + int(completion_tokens)
                hold = None
                if hold_id:
                    hold = await connection.fetchrow(
                        "SELECT * FROM credit_holds WHERE hold_id = $1 FOR UPDATE",
                        str(hold_id),
                    )
                    if hold is not None:
                        await connection.execute(
                            "DELETE FROM credit_holds WHERE hold_id = $1",
                            str(hold_id),
                        )
                if ok and hold is not None:
                    amount = max(0, int(hold["credits"] or 0))
                    estimated_cost = round(
                        max(
                            0.0,
                            float(
                                actual_ai_cost_usd
                                if actual_ai_cost_usd is not None
                                else hold["estimated_ai_cost_usd"]
                            )
                            or 0.0
                        ),
                        6,
                    )
                    today_key = _today_key(now)
                    if str(user.get("daily_credits_used_date", "") or "") != today_key:
                        user["daily_credits_used"] = 0
                        user["daily_credits_used_date"] = today_key
                    user["credit_balance"] = max(0, int(user.get("credit_balance", 0) or 0) - amount)
                    user["token_balance"] = int(user["credit_balance"])
                    user["credits_spent"] = int(user.get("credits_spent", 0) or 0) + amount
                    user["lifetime_tokens_spent"] = int(user.get("credits_spent", 0) or 0)
                    user["daily_credits_used"] = int(user.get("daily_credits_used", 0) or 0) + amount
                    user["ai_budget_spent_usd"] = round(
                        float(user.get("ai_budget_spent_usd", 0.0) or 0.0) + estimated_cost,
                        6,
                    )
                    self._append_transaction_log_locked(
                        user,
                        tx_type="debit",
                        service_key=service_key,
                        amount=amount,
                        balance_after=int(user.get("credit_balance", 0) or 0),
                        estimated_ai_cost_usd=estimated_cost,
                        note=note,
                    )
                user["updated_at"] = _iso(now)
                await self._db_write_user_locked(connection, user)
                return user

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
        current_balance = max(0, int(user.get("credit_balance", 0) or 0))
        target_reset = _next_token_refill(now)
        target_monthly_credits = premium_monthly_credits() if normalized_plan == "premium" else 0
        target_balance = (
            int(credits)
            if isinstance(credits, int) and credits >= 0
            else (target_monthly_credits if normalized_plan == "premium" else current_balance)
        )
        premium_started_at = _iso(now) if normalized_plan == "premium" else str(user.get("premium_started_at", "") or "")
        last_credit_reset = _iso(now) if normalized_plan == "premium" else str(user.get("last_credit_reset", "") or "")
        next_credit_reset_at = _iso(_add_months(now, 1)) if normalized_plan == "premium" else ""
        credits_added_delta = max(0, target_balance if normalized_plan == "premium" else 0)

        if self._pool is not None:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        "SELECT * FROM ai_users WHERE user_id = $1 FOR UPDATE",
                        int(user_id),
                    )
                    if row is None:
                        raise RuntimeError("AI foydalanuvchi plani yangilanmadi.")
                    updated = self._normalize_user_locked(
                        self._serialize_row(row),
                        username=username,
                        full_name=full_name,
                    )
                    updated["current_plan"] = normalized_plan
                    updated["selected_plan"] = MODEL_ALIAS_AUTO
                    updated["selected_model"] = MODEL_ALIAS_AUTO
                    updated["credit_balance"] = int(target_balance)
                    updated["token_balance"] = int(target_balance)
                    updated["monthly_credits"] = int(target_monthly_credits)
                    updated["free_requests_used"] = 0
                    updated["free_reset_date"] = _iso(target_reset)
                    updated["reset_date"] = next_credit_reset_at or _iso(target_reset)
                    updated["free_media_trial_used"] = False
                    updated["free_media_trial_cycle_end"] = updated["free_reset_date"]
                    updated["daily_credit_cap"] = premium_daily_credit_cap()
                    updated["daily_credits_used"] = 0
                    updated["daily_credits_used_date"] = _today_key(now)
                    updated["ai_budget_cap_usd"] = premium_safe_ai_budget_usd()
                    updated["ai_budget_spent_usd"] = 0.0 if normalized_plan == "premium" else float(
                        updated.get("ai_budget_spent_usd", 0.0) or 0.0
                    )
                    updated["premium_started_at"] = premium_started_at
                    updated["last_credit_reset"] = last_credit_reset
                    updated["next_credit_reset_at"] = next_credit_reset_at
                    updated["credits_added"] = int(updated.get("credits_added", 0) or 0) + credits_added_delta
                    updated["lifetime_tokens_earned"] = int(updated.get("credits_added", 0) or 0)
                    updated["updated_at"] = _iso(now)
                    await self._db_write_user_locked(connection, updated)
                    return updated

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
            record["credit_balance"] = int(target_balance)
            record["token_balance"] = int(target_balance)
            record["monthly_credits"] = int(target_monthly_credits)
            record["free_requests_used"] = 0
            record["free_reset_date"] = _iso(target_reset)
            record["reset_date"] = next_credit_reset_at or _iso(target_reset)
            record["free_media_trial_used"] = False
            record["free_media_trial_cycle_end"] = record["free_reset_date"]
            record["daily_credit_cap"] = premium_daily_credit_cap()
            record["daily_credits_used"] = 0
            record["daily_credits_used_date"] = _today_key(now)
            record["ai_budget_cap_usd"] = premium_safe_ai_budget_usd()
            if normalized_plan == "premium":
                record["ai_budget_spent_usd"] = 0.0
            record["premium_started_at"] = premium_started_at
            record["last_credit_reset"] = last_credit_reset
            record["next_credit_reset_at"] = next_credit_reset_at
            record["credits_added"] = int(record.get("credits_added", 0) or 0) + credits_added_delta
            record["lifetime_tokens_earned"] = int(record.get("credits_added", 0) or 0)
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
                async with connection.transaction():
                    row = await connection.fetchrow(
                        "SELECT * FROM ai_users WHERE user_id = $1 FOR UPDATE",
                        int(user_id),
                    )
                    if row is None:
                        raise RuntimeError("AI foydalanuvchi topilmadi.")
                    updated = self._normalize_user_locked(
                        self._serialize_row(row),
                        username="",
                        full_name="",
                    )
                    updated["credit_balance"] = int(credits)
                    updated["token_balance"] = int(credits)
                    updated["updated_at"] = _iso(_utc_now())
                    await self._db_write_user_locked(connection, updated)
                    return updated

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            record = users.get(str(int(user_id)))
            if not isinstance(record, dict):
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            record["credit_balance"] = int(credits)
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
        service_key: str = "",
        note: str = "",
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
                    user = self._normalize_user_locked(
                        self._serialize_row(row),
                        username=username,
                        full_name=full_name,
                    )
                    balance = int(user.get("credit_balance", 0) or 0)
                    charged_amount = min(balance, token_amount)
                    if charged_amount <= 0:
                        return user
                    user["credit_balance"] = balance - charged_amount
                    user["token_balance"] = int(user["credit_balance"])
                    user["credits_spent"] = int(user.get("credits_spent", 0) or 0) + charged_amount
                    user["lifetime_tokens_spent"] = int(user.get("credits_spent", 0) or 0)
                    self._append_transaction_log_locked(
                        user,
                        tx_type="debit",
                        service_key=service_key,
                        amount=charged_amount,
                        balance_after=int(user["credit_balance"]),
                        note=note,
                    )
                    await self._db_write_user_locked(connection, user)
                return user

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            record = users.get(str(int(user_id)))
            if not isinstance(record, dict):
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            self._normalize_user_locked(record, username=username, full_name=full_name)
            charged_amount = min(int(record.get("credit_balance", 0) or 0), token_amount)
            if charged_amount <= 0:
                return json.loads(json.dumps(record))
            record["credit_balance"] = int(record.get("credit_balance", 0) or 0) - charged_amount
            record["token_balance"] = int(record["credit_balance"])
            record["credits_spent"] = int(record.get("credits_spent", 0) or 0) + charged_amount
            record["lifetime_tokens_spent"] = int(record.get("credits_spent", 0) or 0)
            self._append_transaction_log_locked(
                record,
                tx_type="debit",
                service_key=service_key,
                amount=charged_amount,
                balance_after=int(record["credit_balance"]),
                note=note,
            )
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
        service_key: str = "",
        note: str = "",
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
                    user = self._normalize_user_locked(
                        self._serialize_row(row),
                        username=username,
                        full_name=full_name,
                    )
                    user["credit_balance"] = int(user.get("credit_balance", 0) or 0) + token_amount
                    user["token_balance"] = int(user["credit_balance"])
                    user["credits_added"] = int(user.get("credits_added", 0) or 0) + token_amount
                    user["lifetime_tokens_earned"] = int(user.get("credits_added", 0) or 0)
                    self._append_transaction_log_locked(
                        user,
                        tx_type="credit",
                        service_key=service_key,
                        amount=token_amount,
                        balance_after=int(user["credit_balance"]),
                        note=note,
                    )
                    await self._db_write_user_locked(connection, user)
                return user

        await self._ensure_loaded()
        async with self._lock:
            users = self._data.setdefault("users", {})
            record = users.get(str(int(user_id)))
            if not isinstance(record, dict):
                raise RuntimeError("AI foydalanuvchi topilmadi.")
            self._normalize_user_locked(record, username=username, full_name=full_name)
            record["credit_balance"] = int(record.get("credit_balance", 0) or 0) + token_amount
            record["token_balance"] = int(record["credit_balance"])
            record["credits_added"] = int(record.get("credits_added", 0) or 0) + token_amount
            record["lifetime_tokens_earned"] = int(record.get("credits_added", 0) or 0)
            self._append_transaction_log_locked(
                record,
                tx_type="credit",
                service_key=service_key,
                amount=token_amount,
                balance_after=int(record["credit_balance"]),
                note=note,
            )
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
                            credit_balance = credit_balance + $3,
                            token_balance = token_balance + $3,
                            credits_added = credits_added + $3,
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
                            credit_balance = credit_balance + $2,
                            token_balance = token_balance + $2,
                            credits_added = credits_added + $2,
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
            referred["credit_balance"] = int(referred.get("credit_balance", 0) or 0) + invitee_bonus
            referred["token_balance"] = int(referred["credit_balance"])
            referred["credits_added"] = int(referred.get("credits_added", 0) or 0) + invitee_bonus
            referred["lifetime_tokens_earned"] = int(referred.get("credits_added", 0) or 0)
            referred["updated_at"] = _iso(_utc_now())

            referrer["referral_count"] = int(referrer.get("referral_count", 0) or 0) + 1
            referrer["credit_balance"] = int(referrer.get("credit_balance", 0) or 0) + inviter_bonus
            referrer["token_balance"] = int(referrer["credit_balance"])
            referrer["credits_added"] = int(referrer.get("credits_added", 0) or 0) + inviter_bonus
            referrer["lifetime_tokens_earned"] = int(referrer.get("credits_added", 0) or 0)
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
                        user_row = await connection.fetchrow(
                            "SELECT * FROM ai_users WHERE user_id = $1 FOR UPDATE",
                            int(request_row["user_id"]),
                        )
                        if user_row is None:
                            return {"ok": False, "reason": "missing"}
                        user = self._normalize_user_locked(
                            self._serialize_row(user_row),
                            username=str(request_row["username"] or ""),
                            full_name=str(request_row["full_name"] or ""),
                        )
                        now = _utc_now()
                        user["current_plan"] = "premium"
                        user["selected_plan"] = MODEL_ALIAS_AUTO
                        user["selected_model"] = MODEL_ALIAS_AUTO
                        user["credit_balance"] = int(awarded_tokens)
                        user["token_balance"] = int(awarded_tokens)
                        user["monthly_credits"] = premium_monthly_credits()
                        user["free_requests_used"] = 0
                        user["free_reset_date"] = _iso(_next_token_refill(now))
                        user["reset_date"] = _iso(_add_months(now, 1))
                        user["free_media_trial_used"] = False
                        user["free_media_trial_cycle_end"] = user["free_reset_date"]
                        user["premium_started_at"] = _iso(now)
                        user["last_credit_reset"] = _iso(now)
                        user["next_credit_reset_at"] = _iso(_add_months(now, 1))
                        user["daily_credit_cap"] = premium_daily_credit_cap()
                        user["daily_credits_used"] = 0
                        user["daily_credits_used_date"] = _today_key(now)
                        user["ai_budget_cap_usd"] = premium_safe_ai_budget_usd()
                        user["ai_budget_spent_usd"] = 0.0
                        user["credits_added"] = int(user.get("credits_added", 0) or 0) + int(awarded_tokens)
                        user["lifetime_tokens_earned"] = int(user.get("credits_added", 0) or 0)
                        user["updated_at"] = _iso(now)
                        await self._db_write_user_locked(connection, user)
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
                user["current_plan"] = "premium"
                user["selected_plan"] = MODEL_ALIAS_AUTO
                user["selected_model"] = MODEL_ALIAS_AUTO
                user["credit_balance"] = int(awarded_tokens)
                user["token_balance"] = int(awarded_tokens)
                user["monthly_credits"] = premium_monthly_credits()
                user["free_requests_used"] = 0
                user["free_reset_date"] = _iso(_next_token_refill(now))
                user["reset_date"] = _iso(_add_months(now, 1))
                user["free_media_trial_used"] = False
                user["free_media_trial_cycle_end"] = user["free_reset_date"]
                user["premium_started_at"] = _iso(now)
                user["last_credit_reset"] = _iso(now)
                user["next_credit_reset_at"] = _iso(_add_months(now, 1))
                user["daily_credit_cap"] = premium_daily_credit_cap()
                user["daily_credits_used"] = 0
                user["daily_credits_used_date"] = _today_key(now)
                user["ai_budget_cap_usd"] = premium_safe_ai_budget_usd()
                user["ai_budget_spent_usd"] = 0.0
                user["credits_added"] = int(user.get("credits_added", 0) or 0) + awarded_tokens
                user["lifetime_tokens_earned"] = int(user.get("credits_added", 0) or 0)
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
