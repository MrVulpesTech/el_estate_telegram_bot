"""
User commands and URL processing handlers.
Includes /start, /crop, /retry, URL handler; uses scraping service and stats.
"""

import os
import asyncio
import time
import json
import shutil
from typing import Optional

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, FSInputFile, InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import redis.asyncio as aioredis

from ..services.scraping import scrape_images
from ..services import stats as stats_service


DEFAULT_CROP_PERCENTAGE = 15


class UserState(StatesGroup):
    waiting_for_url = State()
    processing_url = State()


def setup_user_router(redis: aioredis.Redis) -> Router:
    router = Router()

    async def _get_user_data(user_id: int) -> dict:
        raw = await redis.get(f"el_estate_bot:user:{user_id}")
        return json.loads(raw) if raw else {}

    async def _save_user_data(user_id: int, data: dict) -> None:
        await redis.set(f"el_estate_bot:user:{user_id}", json.dumps(data), ex=30 * 24 * 3600)

    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.set_state(UserState.waiting_for_url)
        # Refresh username <-> id mapping when the user starts a chat
        try:
            user = message.from_user
            if user:
                uid = user.id
                uname = ("@" + user.username.lower()) if getattr(user, "username", None) else None
                full_name = (user.full_name or None) if getattr(user, "full_name", None) else None
                if uname:
                    await redis.set(f"username_to_id:{uname}", str(uid), ex=30 * 24 * 3600)
                    await redis.set(f"id_to_username:{uid}", uname, ex=30 * 24 * 3600)
                if full_name:
                    await redis.set(f"id_to_fullname:{uid}", full_name, ex=30 * 24 * 3600)
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
        # Refresh mapping on any interaction
        try:
            user = message.from_user
            uname = ("@" + user.username.lower()) if getattr(user, "username", None) else None
            full_name = (user.full_name or None) if getattr(user, "full_name", None) else None
            if uname:
                await redis.set(f"username_to_id:{uname}", str(user_id), ex=30 * 24 * 3600)
                await redis.set(f"id_to_username:{user_id}", uname, ex=30 * 24 * 3600)
            if full_name:
                await redis.set(f"id_to_fullname:{user_id}", full_name, ex=30 * 24 * 3600)
        except Exception:
            pass
        await state.set_state(UserState.processing_url)
        data = await _get_user_data(user_id)
        crop = data.get("crop_percentage", DEFAULT_CROP_PERCENTAGE)
        selenium_url = os.getenv("SELENIUM_URL", "http://localhost:4444/wd/hub")

        status = await message.answer("Збираю зображення, зачекайте…")
        images, user_dir = await scrape_images(url, user_id, selenium_url, crop_percent=crop)

        if images:
            # Increment stats on success
            await stats_service.increment(redis, user_id)
            # Send images in groups of 10
            for i in range(0, len(images), 10):
                group = images[i : i + 10]
                media = [InputMediaPhoto(media=FSInputFile(p)) for p in group]
                retries = 5
                attempt = 0
                while attempt < retries:
                    try:
                        await message.bot.send_media_group(chat_id=message.chat.id, media=media)
                        break
                    except TelegramRetryAfter as e:
                        attempt += 1
                        await asyncio.sleep(e.retry_after + 1)
                    except Exception:
                        attempt += 1
                        await asyncio.sleep(3)
            await status.edit_text(f"✅ Готово. Надіслано {len(images)} зображень.")
            data.update({"last_url": url, "last_images_count": len(images), "last_processed_time": int(time.time()), "crop_percentage": crop})
            await _save_user_data(user_id, data)
        else:
            await status.edit_text("❌ Не вдалося знайти зображення для цього посилання.")
            await state.set_state(UserState.waiting_for_url)

        # Cleanup user directory to free disk space
        try:
            if user_dir and os.path.isdir(user_dir):
                shutil.rmtree(user_dir)
        except Exception:
            pass

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
                inline_keyboard=[[InlineKeyboardButton(text="0%", callback_data="set_crop:0"), InlineKeyboardButton(text="5%", callback_data="set_crop:5"), InlineKeyboardButton(text="10%", callback_data="set_crop:10"), InlineKeyboardButton(text="15%", callback_data="set_crop:15")]]
            ),
        )
        await callback.answer("Оновлено")

    return router


