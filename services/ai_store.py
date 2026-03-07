from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware

from services.ai_gateway import MODEL_ALIAS_AUTO, clamp_selected_plan, effective_selected_plan

try:
    import asyncpg
except Exception:  # pragma: no cover
    asyncpg = None

DEFAULT_AI_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "ai_store.json"


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
    normalized = (plan or "free").strip().lower()
    if normalized == "premium":
        return max(1, _read_int("AI_PREMIUM_MONTHLY_CREDITS", 1500))
    if normalized == "pro":
        return max(1, _read_int("AI_PRO_MONTHLY_CREDITS", 3000))
    return max(1, _read_int("AI_FREE_DAILY_REQUESTS", 20))


def _plan_rpm(plan: str) -> int:
    normalized = (plan or "free").strip().lower()
    if normalized == "premium":
        return max(1, _read_int("AI_PREMIUM_RPM", 30))
    if normalized == "pro":
        return max(1, _read_int("AI_PRO_RPM", 60))
    return max(1, _read_int("AI_FREE_RPM", 12))


def _free_daily_requests() -> int:
    return max(1, _read_int("AI_FREE_DAILY_REQUESTS", 20))


def _free_cooldown_seconds() -> int:
    return max(0, _read_int("AI_FREE_COOLDOWN_SECONDS", 5))


def _context_messages_limit() -> int:
    return max(4, _read_int("AI_CONTEXT_MESSAGES", 12))


def _normalize_selected_model(value: Any) -> str:
    normalized = str(value or MODEL_ALIAS_AUTO).strip().lower()
    return normalized or MODEL_ALIAS_AUTO


def _next_daily_reset(now: datetime | None = None) -> datetime:
    base = now or _utc_now()
    next_day = (base + timedelta(days=1)).date()
    return datetime(
        year=next_day.year,
        month=next_day.month,
        day=next_day.day,
        tzinfo=timezone.utc,
    )


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
            "token_balance": _free_daily_requests(),
            "free_requests_used": 0,
            "free_reset_date": _iso(_next_daily_reset(now)),
            "reset_date": _iso(_next_daily_reset(now)),
            "last_request_at": "",
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
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
        current_plan = str(user.get("current_plan", "free") or "free").strip().lower()
        if current_plan not in {"free", "premium", "pro"}:
            current_plan = "free"
        user["current_plan"] = current_plan
        user["selected_plan"] = clamp_selected_plan(
            str(user.get("selected_plan", MODEL_ALIAS_AUTO) or MODEL_ALIAS_AUTO),
            current_plan,
        )
        user["selected_model"] = _normalize_selected_model(
            user.get("selected_model", MODEL_ALIAS_AUTO)
        )

        free_reset_date = _parse_dt(user.get("free_reset_date"))
        if now >= free_reset_date:
            user["free_requests_used"] = 0
            user["free_reset_date"] = _iso(_next_daily_reset(now))
            if current_plan == "free":
                user["token_balance"] = _free_daily_requests()

        reset_date = _parse_dt(user.get("reset_date"))
        if current_plan == "free":
            if now >= reset_date:
                user["token_balance"] = _free_daily_requests()
                user["reset_date"] = _iso(_next_daily_reset(now))
            else:
                remaining = max(
                    0,
                    _free_daily_requests() - int(user.get("free_requests_used", 0)),
                )
                user["token_balance"] = remaining
        elif now >= reset_date:
            user["token_balance"] = _plan_monthly_credits(current_plan)
            user["reset_date"] = _iso(_next_monthly_reset(now))

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
                    _free_daily_requests(),
                    _next_daily_reset(),
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
        token_balance = int(row["token_balance"] or 0)
        free_requests_used = int(row["free_requests_used"] or 0)
        free_reset_date = _parse_dt(row["free_reset_date"])
        reset_date = _parse_dt(row["reset_date"])

        if now >= free_reset_date:
            free_requests_used = 0
            free_reset_date = _next_daily_reset(now)
            await connection.execute(
                """
                UPDATE ai_users
                SET free_requests_used = 0,
                    free_reset_date = $2,
                    token_balance = CASE
                        WHEN current_plan = 'free' THEN $3
                        ELSE token_balance
                    END,
                    updated_at = NOW()
                WHERE user_id = $1
                """,
                int(row["user_id"]),
                free_reset_date,
                _free_daily_requests(),
            )
            if current_plan == "free":
                token_balance = _free_daily_requests()

        if current_plan == "free":
            if now >= reset_date:
                token_balance = _free_daily_requests()
                reset_date = _next_daily_reset(now)
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
            else:
                token_balance = max(0, _free_daily_requests() - free_requests_used)
                await connection.execute(
                    """
                    UPDATE ai_users
                    SET token_balance = $2,
                        updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    int(row["user_id"]),
                    token_balance,
                )
        elif now >= reset_date:
            token_balance = _plan_monthly_credits(current_plan)
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
            "current_plan": str(row["current_plan"] or "free"),
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

        if effective_plan == "free":
            free_used = int(user.get("free_requests_used", 0) or 0)
            if free_used >= _free_daily_requests():
                reset_at = _parse_dt(user.get("free_reset_date"))
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
                    if str(user.get("current_plan", "free")).strip().lower() == "free":
                        user["token_balance"] = max(
                            0,
                            _free_daily_requests() - int(user["free_requests_used"]),
                        )
                else:
                    user["token_balance"] = max(
                        0,
                        int(user.get("token_balance", 0) or 0) - int(credits_used),
                    )
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
                                token_balance = CASE
                                    WHEN current_plan = 'free' THEN GREATEST(0, $2 - (free_requests_used + 1))
                                    ELSE token_balance
                                END,
                                total_prompt_tokens = total_prompt_tokens + $3,
                                total_completion_tokens = total_completion_tokens + $4,
                                last_request_at = NOW(),
                                updated_at = NOW()
                            WHERE user_id = $1
                            """,
                            int(user_id),
                            _free_daily_requests(),
                            int(prompt_tokens),
                            int(completion_tokens),
                        )
                    else:
                        await connection.execute(
                            """
                            UPDATE ai_users
                            SET token_balance = GREATEST(0, token_balance - $2),
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
        normalized_plan = (plan or "free").strip().lower()
        if normalized_plan not in {"free", "premium", "pro"}:
            raise ValueError("Plan faqat free, premium yoki pro bo'lishi kerak.")
        await self.ensure_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
        )
        now = _utc_now()
        if normalized_plan == "free":
            target_balance = _free_daily_requests()
            target_reset = _next_daily_reset(now)
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
                        updated_at = NOW()
                    WHERE user_id = $1
                    """,
                    int(user_id),
                    normalized_plan,
                    int(target_balance),
                    _next_daily_reset(now),
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
            record["free_reset_date"] = _iso(_next_daily_reset(now))
            record["reset_date"] = _iso(target_reset)
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
