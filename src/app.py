"""
Application entrypoint: initializes aiogram bot/dispatcher, Redis FSM storage,
structured logging, health endpoint, routers, and graceful shutdown.
Changes: custom aiohttp session with resilient connection settings (longer timeouts,
connection pooling) to handle Telegram API connection resets.
"""

import asyncio
import json
import logging
import os
import signal
from contextlib import suppress
from typing import Optional

import redis.asyncio as aioredis
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode as AiogramParseMode
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from redis.exceptions import ReadOnlyError, ResponseError

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


async def _redis_is_writable(redis: aioredis.Redis) -> bool:
    """Checks whether SET works. Some providers expose read-only replicas."""
    try:
        key = f"{REDIS_KEY_PREFIX}:writetest"
        # ex=5 to avoid leaving junk keys around
        await redis.set(key, "1", ex=5)
        return True
    except (ReadOnlyError, ResponseError):
        return False
    except Exception:
        # If anything else happens, consider it not writable to be safe
        return False


async def _monitor_redis_and_fallback(
    redis: aioredis.Redis,
    dp: Dispatcher,
    bot: Bot,
    tech_admin_ids: list[int],
    shutdown_event: asyncio.Event,
    interval_s: int = 20,
) -> None:
    """Periodically verify Redis writability; if it turns read-only at runtime,
    alert admins and request graceful restart.
    """
    logger = logging.getLogger(__name__)
    while True:
        try:
            await asyncio.sleep(max(5, interval_s))
            writable = await _redis_is_writable(redis)
            if not writable and isinstance(dp.storage, RedisStorage):
                msg = "redis.readonly detected at runtime; requesting graceful restart"
                logger.error(msg)
                for uid in tech_admin_ids:
                    with suppress(Exception):
                        await bot.send_message(uid, f"❗ {msg}")
                try:
                    shutdown_event.set()
                except Exception:
                    pass
                break
        except Exception as exc:
            logger.error("redis.monitor.failed err=%r", exc)
            await asyncio.sleep(max(5, interval_s))


async def create_health_app(
    redis: aioredis.Redis, selenium_url: Optional[str]
) -> web.Application:
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

    async def root(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "el-estate-bot"})

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", root)
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

    redis_url = os.getenv(REDIS_URL_ENV, "redis://redis:6379/0")
    selenium_url = os.getenv(SELENIUM_URL_ENV)
    health_port = int(os.getenv(HEALTH_PORT_ENV, "8080"))

    redis = await _init_redis(redis_url)

    key_builder = DefaultKeyBuilder(prefix=REDIS_KEY_PREFIX)
    if await _redis_is_writable(redis):
        storage = RedisStorage(redis=redis, key_builder=key_builder)
    else:
        logging.getLogger(__name__).error(
            "redis.readonly detected; using MemoryStorage for FSM"
        )
        storage = MemoryStorage()

    # Configure Bot HTTP client with resilient connection settings
    # Longer timeouts and connection pooling to handle network resets
    tg_connect_timeout = int(os.getenv("TG_CONNECT_TIMEOUT_S", "15"))
    tg_read_timeout = int(os.getenv("TG_READ_TIMEOUT_S", "60"))
    tg_total_timeout = int(os.getenv("TG_TOTAL_TIMEOUT_S", "120"))
    
    connector = TCPConnector(
        limit=100,
        limit_per_host=10,
        ttl_dns_cache=300,
        force_close=False,  # Keep connections alive
        enable_cleanup_closed=True,
    )
    timeout = ClientTimeout(
        connect=tg_connect_timeout,
        sock_read=tg_read_timeout,
        total=tg_total_timeout,
    )
    bot_session = ClientSession(connector=connector, timeout=timeout)
    
    bot = Bot(
        token=bot_token,
        session=bot_session,
        default=DefaultBotProperties(parse_mode=AiogramParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)

    # Send startup notification to tech admins if configured
    try:
        tech_admins_env = os.getenv("TECH_ADMIN_IDS", "")
        tech_admin_ids = []
        for part in tech_admins_env.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                tech_admin_ids.append(int(part))
            except ValueError:
                continue
        if tech_admin_ids:
            # Install a logging handler that forwards ERROR logs to Telegram
            class TelegramErrorHandler(logging.Handler):
                def __init__(
                    self, bot_obj: Bot, recipients: list[int], loop: asyncio.AbstractEventLoop
                ) -> None:
                    super().__init__(level=logging.ERROR)
                    self._bot = bot_obj
                    self._recipients = recipients
                    self._loop = loop

                def emit(self, record: logging.LogRecord) -> None:  # type: ignore[override]
                    try:
                        # Ignore noisy network scanner errors from aiohttp server
                        if record.name == "aiohttp.server":
                            return
                        if record.levelno < logging.ERROR:
                            return
                        msg = f"ERROR {record.name}: {record.getMessage()}"
                        # Schedule safely from any thread
                        async def _send_all() -> None:
                            for uid in self._recipients:
                                try:
                                    await self._bot.send_message(uid, msg[:3500])
                                except Exception:
                                    continue

                        try:
                            # If we are in the running loop
                            asyncio.get_running_loop()
                            self._loop.create_task(_send_all())
                        except RuntimeError:
                            # From non-async context/thread
                            asyncio.run_coroutine_threadsafe(_send_all(), self._loop)
                    except Exception:
                        pass

            # Use the app loop for thread-safe scheduling
            app_loop = asyncio.get_running_loop()
            root.addHandler(TelegramErrorHandler(bot, tech_admin_ids, app_loop))

            for uid in tech_admin_ids:
                with suppress(Exception):
                    await bot.send_message(uid, "🔔 Bot started and is online.")
    except Exception:
        pass

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
        logging.getLogger(__name__).info(
            json.dumps({"signal": name, "event": "shutdown"})
        )
        try:
            shutdown_event.set()
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    with suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGTERM, _handle_signal, "SIGTERM")
    with suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, _handle_signal, "SIGINT")

    # Start Redis monitor in background (alerts + runtime fallback)
    try:
        tech_admins_env = os.getenv("TECH_ADMIN_IDS", "")
        tech_admin_ids_for_monitor: list[int] = []
        for part in tech_admins_env.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                tech_admin_ids_for_monitor.append(int(part))
            except ValueError:
                continue
        asyncio.create_task(
            _monitor_redis_and_fallback(
                redis, dp, bot, tech_admin_ids_for_monitor, shutdown_event
            )
        )
    except Exception:
        pass

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
            # Close bot's custom session if it exists
            if hasattr(bot, "session") and bot.session:
                await bot.session.close()
        with suppress(Exception):
            await runner.cleanup()
        with suppress(Exception):
            await redis.close()


def main() -> None:
    asyncio.run(start())


if __name__ == "__main__":
    main()
