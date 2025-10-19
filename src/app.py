"""
Application entrypoint: initializes aiogram bot/dispatcher, Redis FSM storage,
structured logging, health endpoint, routers, and graceful shutdown.
"""

import asyncio
import logging
import os
import json
from contextlib import suppress
from typing import Optional
import signal

from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode as AiogramParseMode
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
import redis.asyncio as aioredis

from .bot.admin.commands import setup_admin_router
from .bot.middlewares.whitelist import WhitelistMiddleware
from .bot.user.handlers import setup_user_router


BOT_TOKEN_ENV = "BOT_TOKEN"
REDIS_URL_ENV = "REDIS_URL"
SELENIUM_URL_ENV = "SELENIUM_URL"
HEALTH_PORT_ENV = "HEALTH_PORT"

REDIS_KEY_PREFIX = "el_estate_bot"


async def _init_redis(url: str) -> aioredis.Redis:
    # redis.asyncio.from_url is sync and returns a Redis instance
    return aioredis.from_url(url, decode_responses=True)


async def _selenium_status(base_url: Optional[str]) -> bool:
    if not base_url:
        return True
    # Normalizes trailing path and probes /status with short timeout
    try:
        probe_url = base_url.rstrip("/") + "/status"
        timeout = ClientTimeout(total=1.5)
        async with ClientSession(timeout=timeout) as session:
            async with session.get(probe_url) as resp:
                return resp.status == 200
    except Exception:
        return False


async def _redis_ok(redis: aioredis.Redis) -> bool:
    try:
        pong = await redis.ping()
        return bool(pong)
    except Exception:
        return False


async def create_health_app(redis: aioredis.Redis, selenium_url: Optional[str]) -> web.Application:
    app = web.Application()

    async def healthz(_request: web.Request) -> web.Response:
        redis_ok = await _redis_ok(redis)
        selenium_ok = await _selenium_status(selenium_url)
        status = 200 if (redis_ok and selenium_ok) else 503
        return web.json_response(
            {
                "ok": status == 200,
                "redis_ok": redis_ok,
                "selenium_ok": selenium_ok,
            },
            status=status,
        )

    app.router.add_get("/healthz", healthz)
    return app


async def start() -> None:
    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
            payload = {
                "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            return json.dumps(payload, ensure_ascii=False)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(JsonFormatter())
    root.addHandler(stream_handler)

    # File-based rotation under /app/logs
    try:
        from logging.handlers import RotatingFileHandler

        logs_dir = os.path.join("/app", "logs")
        os.makedirs(logs_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=os.path.join(logs_dir, "app.log"),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)
    except Exception:
        pass

    bot_token = os.getenv(BOT_TOKEN_ENV)
    if not bot_token:
        raise RuntimeError(f"Missing {BOT_TOKEN_ENV} environment variable")

    redis_url = os.getenv(REDIS_URL_ENV, "redis://localhost:6379/0")
    selenium_url = os.getenv(SELENIUM_URL_ENV)
    health_port = int(os.getenv(HEALTH_PORT_ENV, "8080"))

    redis = await _init_redis(redis_url)

    key_builder = DefaultKeyBuilder(prefix=REDIS_KEY_PREFIX)
    storage = RedisStorage(redis=redis, key_builder=key_builder)

    bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode=AiogramParseMode.HTML))
    dp = Dispatcher(storage=storage)

    # Middlewares
    dp.message.middleware.register(WhitelistMiddleware(redis))
    dp.callback_query.middleware.register(WhitelistMiddleware(redis))

    # Routers
    dp.include_router(setup_admin_router(redis))
    dp.include_router(setup_user_router(redis))

    # Health server
    health_app = create_health_app(redis, selenium_url)
    if asyncio.iscoroutine(health_app):
        health_app = await health_app  # type: ignore[assignment]
    runner = web.AppRunner(health_app)  # type: ignore[arg-type]
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=health_port)
    await site.start()
    logging.getLogger(__name__).info("/healthz started on port %s", health_port)

    # Graceful shutdown setup
    shutdown_event: asyncio.Event = asyncio.Event()

    def _handle_signal(name: str) -> None:
        logging.getLogger(__name__).info(json.dumps({"signal": name, "event": "shutdown"}))
        try:
            shutdown_event.set()
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    with suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGTERM, _handle_signal, "SIGTERM")
    with suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, _handle_signal, "SIGINT")

    # Run polling until shutdown_event is set
    polling_task = asyncio.create_task(dp.start_polling(bot))
    try:
        await shutdown_event.wait()
        if not polling_task.done():
            polling_task.cancel()
            with suppress(asyncio.CancelledError):
                await polling_task
    finally:
        with suppress(Exception):
            await bot.session.close()
        with suppress(Exception):
            await runner.cleanup()
        with suppress(Exception):
            await redis.close()


def main() -> None:
    asyncio.run(start())


if __name__ == "__main__":
    main()


