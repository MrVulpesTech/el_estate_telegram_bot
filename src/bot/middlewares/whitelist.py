"""
Whitelist middleware enforcing access control via Redis SET `whitelist:users`.
Admins from `ADMIN_IDS` bypass restrictions.
"""

import asyncio
import logging
import os
import time
from typing import Set

import redis.asyncio as aioredis
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

WHITELIST_SET_KEY = "whitelist:users"


def _parse_admin_ids(env_value: str | None) -> Set[int]:
    if not env_value:
        return set()
    out: Set[int] = set()
    for part in env_value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


class WhitelistMiddleware(BaseMiddleware):
    def __init__(self, redis: aioredis.Redis) -> None:
        super().__init__()
        self.redis = redis
        self.admin_ids: Set[int] = _parse_admin_ids(os.getenv("ADMIN_IDS"))
        # In-memory fallback cache for allowed users
        self._cached_allowed_ids: Set[int] = set()
        self._last_cache_refresh: float = 0.0
        # Refresh every 30 seconds; override via env if needed
        try:
            self._cache_refresh_interval_s: int = int(
                os.getenv("WHITELIST_CACHE_REFRESH_S", "30")
            )
        except ValueError:
            self._cache_refresh_interval_s = 30
        self._refresher_task: asyncio.Task | None = None

    async def _refresh_cache(self) -> None:
        try:
            ids = await self.redis.smembers(WHITELIST_SET_KEY)
            # smembers returns strings; normalize to ints when possible
            normalized: Set[int] = set()
            for uid in ids:
                try:
                    normalized.add(int(uid))
                except (ValueError, TypeError):
                    continue
            self._cached_allowed_ids = normalized
            self._last_cache_refresh = time.time()
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "whitelist.cache.refresh_failed err=%r", exc
            )

    def _ensure_refresher_running(self) -> None:
        if self._refresher_task is not None:
            return

        async def _run() -> None:
            # Stagger initial refresh slightly
            await asyncio.sleep(0.1)
            while True:
                await self._refresh_cache()
                await asyncio.sleep(max(5, self._cache_refresh_interval_s))

        try:
            self._refresher_task = asyncio.create_task(_run())
        except RuntimeError:
            # No running loop yet; will start on first call
            self._refresher_task = None

    async def _is_allowed(self, user_id: int) -> bool:
        if user_id in self.admin_ids:
            return True
        try:
            return bool(await self.redis.sismember(WHITELIST_SET_KEY, str(user_id)))
        except Exception as exc:
            # Fallback to cached whitelist if Redis is unavailable or raises WRONGTYPE
            logging.getLogger(__name__).warning(
                "whitelist.check_failed user_id=%s err=%r", user_id, exc
            )
            return user_id in self._cached_allowed_ids

    async def __call__(self, handler, event: TelegramObject, data: dict):  # type: ignore[override]
        # Start background refresher if not running and do a lazy, periodic refresh
        self._ensure_refresher_running()
        now = time.time()
        if now - self._last_cache_refresh > max(5, self._cache_refresh_interval_s):
            await self._refresh_cache()

        user_id: int | None = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        if user_id is None:
            return await handler(event, data)

        # Allow /start so the bot can capture username ↔ id mapping even before allow-listing
        if isinstance(event, Message):
            text = event.text or ""
            if text.startswith("/start"):
                return await handler(event, data)

        if await self._is_allowed(user_id):
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer(
                "Доступ обмежено. Зверніться до адміністратора для внесення до білого списку."
            )
        return None
