"""
Whitelist middleware enforcing access control via Redis SET `whitelist:users`.
Admins from `ADMIN_IDS` bypass restrictions.
"""

import asyncio
import logging
import os
import time
import json
from typing import Set, Iterable

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
        # Treat blank env as unset; default to mounted JSON file
        env_path = os.getenv("WHITELIST_JSON")
        self._backup_path: str = env_path if env_path else "data/whitelist.json"
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
        self._restore_attempted: bool = False

    def _ensure_backup_dir(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._backup_path) or ".", exist_ok=True)
        except Exception:
            pass

    def _load_backup_ids(self) -> Set[int]:
        try:
            if not os.path.exists(self._backup_path):
                return set()
            with open(self._backup_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            out: Set[int] = set()
            for v in raw if isinstance(raw, list) else []:
                try:
                    out.add(int(v))
                except Exception:
                    continue
            return out
        except Exception as exc:
            logging.getLogger(__name__).warning("whitelist.backup.load_failed err=%r", exc)
            return set()

    async def _maybe_restore_from_backup(self) -> None:
        if self._restore_attempted:
            return
        self._restore_attempted = True
        try:
            ids = await self.redis.smembers(WHITELIST_SET_KEY)
            if ids:
                # Union backup into Redis to avoid shrinking if backup has more
                backup_ids = self._load_backup_ids()
                missing = [str(i) for i in backup_ids if str(i) not in ids]
                if missing:
                    try:
                        await self.redis.sadd(WHITELIST_SET_KEY, *missing)
                        logging.getLogger(__name__).info(
                            "whitelist.synced added_missing=%d", len(missing)
                        )
                    except Exception as exc:
                        logging.getLogger(__name__).error(
                            "whitelist.sync_failed err=%r", exc
                        )
                return
        except Exception:
            # If Redis errors, skip restore attempt
            return
        backup_ids = self._load_backup_ids()
        if not backup_ids:
            return
        try:
            await self.redis.sadd(WHITELIST_SET_KEY, *[str(i) for i in backup_ids])
            logging.getLogger(__name__).info(
                "whitelist.restored count=%d", len(backup_ids)
            )
        except Exception as exc:
            logging.getLogger(__name__).error("whitelist.restore_failed err=%r", exc)

    async def _refresh_cache(self) -> None:
        try:
            await self._maybe_restore_from_backup()
            ids = await self.redis.smembers(WHITELIST_SET_KEY)
            # smembers returns strings; normalize to ints when possible
            normalized: Set[int] = set()
            for uid in ids:
                try:
                    normalized.add(int(uid))
                except (ValueError, TypeError):
                    continue
            # Do not replace cache with empty to avoid transient wipes
            if not normalized:
                logging.getLogger(__name__).warning(
                    "whitelist.refresh.empty_skip keeping_cached=%d",
                    len(self._cached_allowed_ids),
                )
                return
            self._cached_allowed_ids = normalized
            self._last_cache_refresh = time.time()
        except Exception as exc:
            logging.getLogger(__name__).error(
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
            logging.getLogger(__name__).error(
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
