from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from services.storage_config import resolve_database_url

try:
    import asyncpg
except Exception:  # pragma: no cover
    asyncpg = None

DEFAULT_ANALYTICS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "analytics.json"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _row_ts(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return str(value or "")


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class AnalyticsStore:
    def __init__(
        self,
        path: Path | None = None,
        database_url: str | None = None,
    ) -> None:
        self.path = path or DEFAULT_ANALYTICS_PATH
        self.database_url = resolve_database_url(database_url)
        self._lock = asyncio.Lock()
        self._loaded = False
        self._data: dict[str, Any] = {}
        self._pool: Any | None = None
        self._dirty = False
        self._last_saved_monotonic = 0.0
        self._save_interval_seconds = max(
            0,
            _read_int("ANALYTICS_LOCAL_SAVE_INTERVAL_SECONDS", 2),
        )

    def _default_data(self) -> dict[str, Any]:
        return {
            "totals": {
                "messages": 0,
                "callbacks": 0,
                "downloads": 0,
                "broadcasts": 0,
            },
            "services": {},
            "commands": {},
            "users": {},
            "broadcast_history": [],
        }

    def is_database_enabled(self) -> bool:
        return bool(self._pool is not None)

    async def startup(self) -> None:
        if self.database_url:
            if asyncpg is None:
                raise RuntimeError("asyncpg o'rnatilmagan. requirements ni yangilang.")
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
        if self._pool is None and self._loaded and self._dirty:
            async with self._lock:
                await self._save_if_due_locked(force=True)
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _ensure_schema(self) -> None:
        if self._pool is None:
            return
        schema_sql = """
        CREATE TABLE IF NOT EXISTS bot_totals (
            key TEXT PRIMARY KEY,
            value BIGINT NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS bot_services (
            key TEXT PRIMARY KEY,
            value BIGINT NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS bot_commands (
            key TEXT PRIMARY KEY,
            value BIGINT NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id BIGINT PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            full_name TEXT NOT NULL DEFAULT '',
            joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            messages BIGINT NOT NULL DEFAULT 0,
            callbacks BIGINT NOT NULL DEFAULT 0,
            commands BIGINT NOT NULL DEFAULT 0,
            downloads BIGINT NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS bot_broadcast_history (
            id BIGSERIAL PRIMARY KEY,
            sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            sent BIGINT NOT NULL DEFAULT 0,
            failed BIGINT NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS bot_meta (
            key TEXT PRIMARY KEY,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        """
        async with self._pool.acquire() as connection:
            await connection.execute(schema_sql)

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
            self._last_saved_monotonic = time.monotonic()

    async def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.path)
        self._dirty = False
        self._last_saved_monotonic = time.monotonic()

    async def _save_if_due_locked(self, *, force: bool = False) -> None:
        if not self._dirty:
            return
        if not force and self._save_interval_seconds > 0:
            elapsed = time.monotonic() - self._last_saved_monotonic
            if elapsed < float(self._save_interval_seconds):
                return
        await self._save_locked()

    def _touch_user_locked(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
    ) -> dict[str, Any]:
        users = self._data.setdefault("users", {})
        key = str(user_id)
        now = _utc_now()
        user = users.get(key)
        if not isinstance(user, dict):
            user = {
                "user_id": user_id,
                "username": username,
                "full_name": full_name,
                "joined_at": now,
                "last_seen": now,
                "messages": 0,
                "callbacks": 0,
                "commands": 0,
                "downloads": 0,
            }
            users[key] = user
        else:
            user["username"] = username
            user["full_name"] = full_name
            user["last_seen"] = now
        return user

    async def _mutate(
        self,
        callback: Callable[[dict[str, Any]], None],
    ) -> None:
        await self._ensure_loaded()
        async with self._lock:
            callback(self._data)
            self._dirty = True
            await self._save_if_due_locked()

    async def _db_touch_user(
        self,
        connection: Any,
        *,
        user_id: int,
        username: str,
        full_name: str,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO bot_users (
                user_id, username, full_name, joined_at, last_seen
            )
            VALUES ($1, $2, $3, NOW(), NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                full_name = EXCLUDED.full_name,
                last_seen = NOW()
            """,
            user_id,
            username,
            full_name,
        )

    async def _db_increment(self, connection: Any, table: str, key: str) -> None:
        await connection.execute(
            f"""
            INSERT INTO {table} (key, value)
            VALUES ($1, 1)
            ON CONFLICT (key) DO UPDATE SET value = {table}.value + 1
            """,
            key,
        )

    async def _db_set_meta(self, connection: Any, key: str, payload: dict[str, Any]) -> None:
        await connection.execute(
            """
            INSERT INTO bot_meta (key, payload)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload
            """,
            key,
            json.dumps(payload, ensure_ascii=False),
        )

    async def track_message(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        command: str = "",
    ) -> None:
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    await self._db_touch_user(
                        connection,
                        user_id=user_id,
                        username=username,
                        full_name=full_name,
                    )
                    await self._db_increment(connection, "bot_totals", "messages")
                    await connection.execute(
                        "UPDATE bot_users SET messages = messages + 1 WHERE user_id = $1",
                        user_id,
                    )
                    if command:
                        await self._db_increment(connection, "bot_commands", command)
                        await connection.execute(
                            "UPDATE bot_users SET commands = commands + 1 WHERE user_id = $1",
                            user_id,
                        )
            return

        def mutate(data: dict[str, Any]) -> None:
            user = self._touch_user_locked(
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            totals = data.setdefault("totals", {})
            totals["messages"] = int(totals.get("messages", 0)) + 1
            user["messages"] = int(user.get("messages", 0)) + 1
            if command:
                commands = data.setdefault("commands", {})
                commands[command] = int(commands.get(command, 0)) + 1
                user["commands"] = int(user.get("commands", 0)) + 1

        await self._mutate(mutate)

    async def track_callback(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        callback_data: str = "",
    ) -> None:
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    await self._db_touch_user(
                        connection,
                        user_id=user_id,
                        username=username,
                        full_name=full_name,
                    )
                    await self._db_increment(connection, "bot_totals", "callbacks")
                    await connection.execute(
                        "UPDATE bot_users SET callbacks = callbacks + 1 WHERE user_id = $1",
                        user_id,
                    )
                    if callback_data.startswith("services:"):
                        service = callback_data.split(":", maxsplit=1)[1].strip().lower()
                        if service and service != "back":
                            await self._db_increment(connection, "bot_services", service)
            return

        def mutate(data: dict[str, Any]) -> None:
            user = self._touch_user_locked(
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            totals = data.setdefault("totals", {})
            totals["callbacks"] = int(totals.get("callbacks", 0)) + 1
            user["callbacks"] = int(user.get("callbacks", 0)) + 1

            if callback_data.startswith("services:"):
                service = callback_data.split(":", maxsplit=1)[1].strip().lower()
                if service and service != "back":
                    services = data.setdefault("services", {})
                    services[service] = int(services.get(service, 0)) + 1

        await self._mutate(mutate)

    async def record_download(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        source: str,
        size: int,
    ) -> None:
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    await self._db_touch_user(
                        connection,
                        user_id=user_id,
                        username=username,
                        full_name=full_name,
                    )
                    await self._db_increment(connection, "bot_totals", "downloads")
                    await connection.execute(
                        "UPDATE bot_users SET downloads = downloads + 1 WHERE user_id = $1",
                        user_id,
                    )
                    await self._db_increment(
                        connection,
                        "bot_services",
                        f"download:{source or 'unknown'}",
                    )
                    await self._db_set_meta(
                        connection,
                        "last_download",
                        {
                            "user_id": user_id,
                            "source": source,
                            "size": int(size),
                            "at": _utc_now(),
                        },
                    )
            return

        def mutate(data: dict[str, Any]) -> None:
            user = self._touch_user_locked(
                user_id=user_id,
                username=username,
                full_name=full_name,
            )
            totals = data.setdefault("totals", {})
            totals["downloads"] = int(totals.get("downloads", 0)) + 1
            user["downloads"] = int(user.get("downloads", 0)) + 1
            services = data.setdefault("services", {})
            key = f"download:{source or 'unknown'}"
            services[key] = int(services.get(key, 0)) + 1
            data["last_download"] = {
                "user_id": user_id,
                "source": source,
                "size": int(size),
                "at": _utc_now(),
            }

        await self._mutate(mutate)

    async def record_broadcast(self, *, sent: int, failed: int) -> None:
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                async with connection.transaction():
                    await self._db_increment(connection, "bot_totals", "broadcasts")
                    await connection.execute(
                        """
                        INSERT INTO bot_broadcast_history (sent_at, sent, failed)
                        VALUES (NOW(), $1, $2)
                        """,
                        int(sent),
                        int(failed),
                    )
            return

        def mutate(data: dict[str, Any]) -> None:
            totals = data.setdefault("totals", {})
            totals["broadcasts"] = int(totals.get("broadcasts", 0)) + 1
            history = data.setdefault("broadcast_history", [])
            history.insert(
                0,
                {
                    "sent_at": _utc_now(),
                    "sent": int(sent),
                    "failed": int(failed),
                },
            )
            del history[10:]

        await self._mutate(mutate)

    async def snapshot(self) -> dict[str, Any]:
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                total_rows = await connection.fetch(
                    "SELECT key, value FROM bot_totals"
                )
                service_rows = await connection.fetch(
                    "SELECT key, value FROM bot_services"
                )
                command_rows = await connection.fetch(
                    "SELECT key, value FROM bot_commands"
                )
                user_rows = await connection.fetch(
                    """
                    SELECT
                        user_id,
                        username,
                        full_name,
                        joined_at,
                        last_seen,
                        messages,
                        callbacks,
                        commands,
                        downloads
                    FROM bot_users
                    """
                )
                broadcast_rows = await connection.fetch(
                    """
                    SELECT sent_at, sent, failed
                    FROM bot_broadcast_history
                    ORDER BY sent_at DESC
                    LIMIT 10
                    """
                )
                last_download_row = await connection.fetchrow(
                    "SELECT payload FROM bot_meta WHERE key = 'last_download'"
                )

            snapshot = self._default_data()
            snapshot["totals"] = {
                str(row["key"]): int(row["value"]) for row in total_rows
            }
            for key in ("messages", "callbacks", "downloads", "broadcasts"):
                snapshot["totals"].setdefault(key, 0)
            snapshot["services"] = {
                str(row["key"]): int(row["value"]) for row in service_rows
            }
            snapshot["commands"] = {
                str(row["key"]): int(row["value"]) for row in command_rows
            }
            snapshot["users"] = {
                str(row["user_id"]): {
                    "user_id": int(row["user_id"]),
                    "username": str(row["username"] or ""),
                    "full_name": str(row["full_name"] or ""),
                    "joined_at": _row_ts(row["joined_at"]),
                    "last_seen": _row_ts(row["last_seen"]),
                    "messages": int(row["messages"] or 0),
                    "callbacks": int(row["callbacks"] or 0),
                    "commands": int(row["commands"] or 0),
                    "downloads": int(row["downloads"] or 0),
                }
                for row in user_rows
            }
            snapshot["broadcast_history"] = [
                {
                    "sent_at": _row_ts(row["sent_at"]),
                    "sent": int(row["sent"] or 0),
                    "failed": int(row["failed"] or 0),
                }
                for row in broadcast_rows
            ]
            if last_download_row is not None:
                payload = last_download_row["payload"]
                if isinstance(payload, str):
                    snapshot["last_download"] = json.loads(payload)
                elif isinstance(payload, dict):
                    snapshot["last_download"] = payload
            return snapshot

        await self._ensure_loaded()
        async with self._lock:
            return json.loads(json.dumps(self._data))

    async def user_ids(self) -> list[int]:
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                rows = await connection.fetch("SELECT user_id FROM bot_users ORDER BY user_id")
            return [int(row["user_id"]) for row in rows]

        snapshot = await self.snapshot()
        users = snapshot.get("users", {})
        result: list[int] = []
        if isinstance(users, dict):
            for key in users:
                try:
                    result.append(int(key))
                except (TypeError, ValueError):
                    continue
        return result

    async def recent_users(self, limit: int = 12) -> list[dict[str, Any]]:
        if self._pool is not None:
            async with self._pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT
                        user_id,
                        username,
                        full_name,
                        joined_at,
                        last_seen,
                        messages,
                        callbacks,
                        commands,
                        downloads
                    FROM bot_users
                    ORDER BY last_seen DESC
                    LIMIT $1
                    """,
                    int(limit),
                )
            return [
                {
                    "user_id": int(row["user_id"]),
                    "username": str(row["username"] or ""),
                    "full_name": str(row["full_name"] or ""),
                    "joined_at": _row_ts(row["joined_at"]),
                    "last_seen": _row_ts(row["last_seen"]),
                    "messages": int(row["messages"] or 0),
                    "callbacks": int(row["callbacks"] or 0),
                    "commands": int(row["commands"] or 0),
                    "downloads": int(row["downloads"] or 0),
                }
                for row in rows
            ]

        snapshot = await self.snapshot()
        users = snapshot.get("users", {})
        if not isinstance(users, dict):
            return []
        rows = [value for value in users.values() if isinstance(value, dict)]
        rows.sort(key=lambda item: str(item.get("last_seen", "")), reverse=True)
        return rows[:limit]


class AnalyticsMiddleware(BaseMiddleware):
    def __init__(self, analytics_store: AnalyticsStore) -> None:
        self.analytics_store = analytics_store

    @staticmethod
    def _skip_analytics_for_message(*, command: str, state_name: str) -> bool:
        normalized_command = command.strip().lower()
        normalized_state = state_name.strip().lower()
        if normalized_command in {"/ai", "/image", "/art", "/pollinations"}:
            return True
        return normalized_state in {
            "aichatstate:waiting_prompt",
            "pollinationsstate:waiting_prompt",
        }

    @staticmethod
    def _skip_analytics_for_callback(callback_data: str) -> bool:
        normalized = str(callback_data or "").strip().lower()
        return normalized.startswith(
            (
                "ai:",
                "pollinations:",
                "services:ai",
                "services:pollinations",
            )
        )

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        data["analytics_store"] = self.analytics_store

        user = getattr(event, "from_user", None)
        if user is not None and not getattr(user, "is_bot", False):
            username = str(getattr(user, "username", "") or "").strip()
            full_name = " ".join(
                part
                for part in [
                    str(getattr(user, "first_name", "") or "").strip(),
                    str(getattr(user, "last_name", "") or "").strip(),
                ]
                if part
            ).strip()

            if isinstance(event, Message):
                text = str(event.text or event.caption or "").strip()
                command = ""
                if text.startswith("/"):
                    command = text.split(maxsplit=1)[0].lower()
                state_name = ""
                fsm_state = data.get("state")
                get_state = getattr(fsm_state, "get_state", None)
                if callable(get_state):
                    state_name = str((await get_state()) or "")
                if not self._skip_analytics_for_message(
                    command=command,
                    state_name=state_name,
                ):
                    await self.analytics_store.track_message(
                        user_id=int(user.id),
                        username=username,
                        full_name=full_name,
                        command=command,
                    )
            elif isinstance(event, CallbackQuery):
                callback_data = str(event.data or "")
                if not self._skip_analytics_for_callback(callback_data):
                    await self.analytics_store.track_callback(
                        user_id=int(user.id),
                        username=username,
                        full_name=full_name,
                        callback_data=callback_data,
                    )

        return await handler(event, data)
