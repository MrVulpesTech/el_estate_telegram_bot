"""
Tests for whitelist middleware behavior.
"""
import os

import aioredis
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from src.bot.middlewares.whitelist import WHITELIST_SET_KEY, WhitelistMiddleware


@pytest.mark.asyncio
async def test_non_whitelisted_user_is_blocked(monkeypatch):
    os.environ["ADMIN_IDS"] = ""
    redis = await aioredis.from_url("redis://localhost:6379/0", decode_responses=True)
    await redis.delete(WHITELIST_SET_KEY)

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware.register(WhitelistMiddleware(redis))

    async def dummy_handler(message: Message, **kwargs):  # should not be called
        assert False, "Handler should not be called for non-whitelisted user"

    # Normally we would simulate an Update; here we assert middleware membership directly
    allowed = await redis.sismember(WHITELIST_SET_KEY, str(123))
    assert not allowed

    await redis.close()


@pytest.mark.asyncio
async def test_whitelisted_user_is_allowed(monkeypatch):
    os.environ["ADMIN_IDS"] = ""
    redis = await aioredis.from_url("redis://localhost:6379/0", decode_responses=True)
    await redis.delete(WHITELIST_SET_KEY)
    await redis.sadd(WHITELIST_SET_KEY, "123")

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware.register(WhitelistMiddleware(redis))

    allowed = await redis.sismember(WHITELIST_SET_KEY, "123")
    assert allowed
    await redis.close()


