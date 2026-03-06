from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

DEFAULT_ANALYTICS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "analytics.json"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class AnalyticsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_ANALYTICS_PATH
        self._lock = asyncio.Lock()
        self._loaded = False
        self._data: dict[str, Any] = {}

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
            await self._save_locked()

    async def track_message(
        self,
        *,
        user_id: int,
        username: str,
        full_name: str,
        command: str = "",
    ) -> None:
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
        await self._ensure_loaded()
        async with self._lock:
            return json.loads(json.dumps(self._data))

    async def user_ids(self) -> list[int]:
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
                await self.analytics_store.track_message(
                    user_id=int(user.id),
                    username=username,
                    full_name=full_name,
                    command=command,
                )
            elif isinstance(event, CallbackQuery):
                await self.analytics_store.track_callback(
                    user_id=int(user.id),
                    username=username,
                    full_name=full_name,
                    callback_data=str(event.data or ""),
                )

        return await handler(event, data)
