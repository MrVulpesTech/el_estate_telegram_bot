"""
User commands and URL processing handlers.
Includes /start, /crop, /retry, URL handler; uses scraping service and stats.
Changes: make URL processing non-blocking by offloading scraping and sending to a background task;
add per-user task deduplication; improve send retry logging (WARN on retries, ERROR on final failure).
"""

import asyncio
import contextlib
import logging
import json
import os
import shutil
import time

import redis.asyncio as aioredis
from redis.exceptions import ReadOnlyError, ResponseError
from aiogram import Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiohttp import ClientTimeout

from ..services import stats as stats_service
from ..services.scraping import scrape_images

DEFAULT_CROP_PERCENTAGE = 15
MEDIA_GROUP_SIZE = int(os.getenv("MEDIA_GROUP_SIZE", "10"))
# Dynamic Telegram HTTP timeout backoff (in seconds)
TG_TIMEOUT_BASE_S = int(os.getenv("TG_TIMEOUT_BASE_S", "60"))
TG_TIMEOUT_STEP_S = int(os.getenv("TG_TIMEOUT_STEP_S", "30"))
TG_TIMEOUT_MAX_S = int(os.getenv("TG_TIMEOUT_MAX_S", "240"))
TG_CONNECT_TIMEOUT_S = int(os.getenv("TG_CONNECT_TIMEOUT_S", "10"))


class UserState(StatesGroup):
    waiting_for_url = State()
    processing_url = State()


def setup_user_router(redis: aioredis.Redis) -> Router:
    router = Router()
    logger = logging.getLogger(__name__)

    # Track per-user running tasks to avoid overlapping long jobs for the same chat
    _user_tasks: dict[int, asyncio.Task] = {}

    async def _get_user_data(user_id: int) -> dict:
        raw = await redis.get(f"el_estate_bot:user:{user_id}")
        return json.loads(raw) if raw else {}

    async def _save_user_data(user_id: int, data: dict) -> None:
        try:
            await redis.set(
                f"el_estate_bot:user:{user_id}", json.dumps(data), ex=30 * 24 * 3600
            )
        except (ReadOnlyError, ResponseError):
            # Ignore if Redis is read-only; user state is non-critical
            pass

    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.set_state(UserState.waiting_for_url)
        # Refresh username <-> id mapping when the user starts a chat
        try:
            user = message.from_user
            if user:
                uid = user.id
                uname = (
                    ("@" + user.username.lower())
                    if getattr(user, "username", None)
                    else None
                )
                full_name = (
                    (user.full_name or None)
                    if getattr(user, "full_name", None)
                    else None
                )
                if uname:
                    await redis.set(
                        f"username_to_id:{uname}", str(uid), ex=30 * 24 * 3600
                    )
                    await redis.set(f"id_to_username:{uid}", uname, ex=30 * 24 * 3600)
                if full_name:
                    await redis.set(
                        f"id_to_fullname:{uid}", full_name, ex=30 * 24 * 3600
                    )
        except Exception:
            pass
        await message.answer(
            "👋 Привіт! Надішли посилання на оголошення OLX або Otodom — я зберу зображення.\n"
            "Використовуй /crop, щоб налаштувати обрізання."
        )

    @router.message(Command("crop"))
    async def cmd_crop_buttons(message: Message) -> None:
        user_id = message.from_user.id
        data = await _get_user_data(user_id)
        crop = data.get("crop_percentage", DEFAULT_CROP_PERCENTAGE)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="0%", callback_data="set_crop:0"),
                    InlineKeyboardButton(text="5%", callback_data="set_crop:5"),
                    InlineKeyboardButton(text="10%", callback_data="set_crop:10"),
                    InlineKeyboardButton(text="15%", callback_data="set_crop:15"),
                ]
            ]
        )
        await message.answer(
            f"Поточна обрізка знизу: {crop}%",
            reply_markup=kb,
        )

    # old parameterized /crop is removed; use buttons instead

    @router.message(Command("retry"))
    async def cmd_retry(message: Message, state: FSMContext) -> None:
        data = await _get_user_data(message.from_user.id)
        last_url = data.get("last_url")
        if not last_url:
            await message.answer("Немає попереднього посилання для повтору.")
            return
        await _process_url(message, state, last_url)

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "🤖 Бот для збору зображень з OLX та Otodom.\n\n"
            "Команди користувача:\n"
            "/start — почати\n"
            "/crop — обрізка знизу (через кнопки)\n"
            "/retry — повтор останнього посилання"
        )

    @router.message(lambda m: bool(m.text) and m.text.startswith("http"))
    async def handle_url(message: Message, state: FSMContext) -> None:
        await _process_url(message, state, message.text)

    async def _process_url(message: Message, state: FSMContext, url: str) -> None:
        user_id = message.from_user.id

        # If there's already a running task for this user, do not start another one
        existing = _user_tasks.get(user_id)
        if existing and not existing.done():
            await message.answer("⏳ Ще обробляю попереднє посилання. Зачекайте, будь ласка…")
            return

        async def _bump_bot_timeout(desired_total_s: int) -> None:
            """Increase Telegram HTTP client timeout if below desired_total_s."""
            try:
                sess = getattr(message.bot, "session", None)
                if not sess:
                    return
                current = getattr(sess, "timeout", None)
                current_total = getattr(current, "total", None) if current else None
                if (
                    current_total is None
                    or (isinstance(current_total, (int, float)) and current_total < desired_total_s)
                ):
                    sess.timeout = ClientTimeout(total=desired_total_s, connect=TG_CONNECT_TIMEOUT_S)
                    logger.info("tg.timeout.bump total=%s", desired_total_s)
            except Exception:
                # Never fail message flow due to timeout tuning
                pass

        # Refresh mapping on any interaction
        try:
            user = message.from_user
            uname = (
                ("@" + user.username.lower())
                if getattr(user, "username", None)
                else None
            )
            full_name = (
                (user.full_name or None) if getattr(user, "full_name", None) else None
            )
            if uname:
                await redis.set(
                    f"username_to_id:{uname}", str(user_id), ex=30 * 24 * 3600
                )
                await redis.set(f"id_to_username:{user_id}", uname, ex=30 * 24 * 3600)
            if full_name:
                await redis.set(
                    f"id_to_fullname:{user_id}", full_name, ex=30 * 24 * 3600
                )
        except Exception:
            pass
        await state.set_state(UserState.processing_url)
        data = await _get_user_data(user_id)
        crop = data.get("crop_percentage", DEFAULT_CROP_PERCENTAGE)
        selenium_url = os.getenv("SELENIUM_URL", "http://localhost:4444/wd/hub")

        status = await message.answer("Збираю зображення, зачекайте…")

        async def _do_scrape_and_send() -> None:
            user_dir = ""
            try:
                images, user_dir = await scrape_images(
                    url, user_id, selenium_url, crop_percent=crop
                )

                if images:
                    # Increment stats on success
                    await stats_service.increment(redis, user_id)
                    # Send images in groups
                    for i in range(0, len(images), MEDIA_GROUP_SIZE):
                        group = images[i : i + MEDIA_GROUP_SIZE]
                        media = [InputMediaPhoto(media=FSInputFile(p)) for p in group]
                        retries = 5
                        attempt = 0
                        last_exc: Exception | None = None
                        while attempt < retries:
                            try:
                                # Dynamically increase Telegram client timeout per attempt
                                desired_total = min(
                                    TG_TIMEOUT_BASE_S + attempt * TG_TIMEOUT_STEP_S, TG_TIMEOUT_MAX_S
                                )
                                await _bump_bot_timeout(desired_total)
                                await message.bot.send_media_group(
                                    chat_id=message.chat.id, media=media
                                )
                                last_exc = None
                                break
                            except TelegramRetryAfter as e:
                                attempt += 1
                                logger.warning(
                                    "send.media.retry_after user_id=%s delay=%s attempt=%d/%d",
                                    user_id,
                                    getattr(e, "retry_after", None),
                                    attempt,
                                    retries,
                                )
                                await asyncio.sleep(e.retry_after + 1)
                            except Exception as exc:
                                attempt += 1
                                last_exc = exc
                                logger.warning(
                                    "send.media.retry user_id=%s err=%r attempt=%d/%d",
                                    user_id,
                                    exc,
                                    attempt,
                                    retries,
                                )
                                await asyncio.sleep(3)
                        if last_exc is not None:
                            # Fallback: send images one-by-one with retries
                            logger.warning(
                                "send.media.group_fallback user_id=%s group_size=%d",
                                user_id,
                                len(group),
                            )
                            for p in group:
                                retries_single = 5
                                attempt_single = 0
                                last_exc_single: Exception | None = None
                                while attempt_single < retries_single:
                                    try:
                                        desired_total_single = min(
                                            TG_TIMEOUT_BASE_S + attempt_single * TG_TIMEOUT_STEP_S,
                                            TG_TIMEOUT_MAX_S,
                                        )
                                        await _bump_bot_timeout(desired_total_single)
                                        await message.bot.send_photo(
                                            chat_id=message.chat.id,
                                            photo=FSInputFile(p),
                                        )
                                        last_exc_single = None
                                        break
                                    except TelegramRetryAfter as e:
                                        attempt_single += 1
                                        logger.warning(
                                            "send.photo.retry_after user_id=%s delay=%s attempt=%d/%d",
                                            user_id,
                                            getattr(e, "retry_after", None),
                                            attempt_single,
                                            retries_single,
                                        )
                                        await asyncio.sleep(e.retry_after + 1)
                                    except Exception as exc2:
                                        attempt_single += 1
                                        last_exc_single = exc2
                                        logger.warning(
                                            "send.photo.retry user_id=%s err=%r attempt=%d/%d",
                                            user_id,
                                            exc2,
                                            attempt_single,
                                            retries_single,
                                        )
                                        await asyncio.sleep(2)
                                if last_exc_single is not None:
                                    logger.error(
                                        "send.photo.failed user_id=%s err=%r",
                                        user_id,
                                        last_exc_single,
                                    )
                            logger.error(
                                "send.media.failed user_id=%s err=%r group_size=%d",
                                user_id,
                                last_exc,
                                len(group),
                            )

                    await status.edit_text(f"✅ Готово. Надіслано {len(images)} зображень.")
                    data.update(
                        {
                            "last_url": url,
                            "last_images_count": len(images),
                            "last_processed_time": int(time.time()),
                            "crop_percentage": crop,
                        }
                    )
                    await _save_user_data(user_id, data)
                else:
                    await status.edit_text(
                        "❌ Не вдалося знайти зображення для цього посилання."
                    )
                    await state.set_state(UserState.waiting_for_url)
            except Exception as exc:
                logger.error(
                    "user.scrape.failed user_id=%s url=%s err=%r", user_id, url, exc
                )
                with contextlib.suppress(Exception):
                    await status.edit_text("❌ Сталася помилка під час обробки.")
                await state.set_state(UserState.waiting_for_url)
            finally:
                # Cleanup user directory to free disk space
                try:
                    if user_dir and os.path.isdir(user_dir):
                        shutil.rmtree(user_dir)
                except Exception:
                    pass
                _user_tasks.pop(user_id, None)

        # Run scraping and sending in background to avoid blocking the update handler
        _user_tasks[user_id] = asyncio.create_task(_do_scrape_and_send())

    @router.callback_query(lambda c: c.data and c.data.startswith("set_crop:"))
    async def set_crop_cb(callback: CallbackQuery) -> None:
        try:
            value = int(callback.data.split(":", 1)[1])
        except Exception:
            await callback.answer("Помилка значення", show_alert=False)
            return
        if value not in (0, 5, 10, 15):
            await callback.answer("Недопустиме значення", show_alert=False)
            return
        user_id = callback.from_user.id
        data = await _get_user_data(user_id)
        data["crop_percentage"] = value
        await _save_user_data(user_id, data)
        await callback.message.edit_text(
            f"Поточна обрізка знизу: {value}%",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="0%", callback_data="set_crop:0"),
                        InlineKeyboardButton(text="5%", callback_data="set_crop:5"),
                        InlineKeyboardButton(text="10%", callback_data="set_crop:10"),
                        InlineKeyboardButton(text="15%", callback_data="set_crop:15"),
                    ]
                ]
            ),
        )
        await callback.answer("Оновлено")

    return router
