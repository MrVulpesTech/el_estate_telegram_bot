"""
Whitelist middleware enforcing access control via Redis SET `whitelist:users`.
Admins from `ADMIN_IDS` bypass restrictions.
"""

import os
from typing import Set

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
import redis.asyncio as aioredis


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

    async def _is_allowed(self, user_id: int) -> bool:
        if user_id in self.admin_ids:
            return True
        try:
            return bool(await self.redis.sismember(WHITELIST_SET_KEY, str(user_id)))
        except Exception:
            # Fail-closed: deny if Redis is unavailable
            return False

    async def __call__(self, handler, event: TelegramObject, data: dict):  # type: ignore[override]
        user_id: int | None = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id

        if user_id is None:
            return await handler(event, data)

        if await self._is_allowed(user_id):
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer("Доступ обмежено. Зверніться до адміністратора для внесення до білого списку.")
        return None


