"""
Admin commands: /admin, /allow, /allow_username, /allow_from_forward,
/deny, /allowed, /stats, /setname.
"""

import os
import json
import logging

import redis.asyncio as aioredis
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..services import stats as stats_service
from .table_render import render_allowed_users, render_usage_tables

WHITELIST_SET_KEY = "whitelist:users"


def _parse_admin_ids() -> set[int]:
    value = os.getenv("ADMIN_IDS", "")
    out: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def setup_admin_router(redis: aioredis.Redis) -> Router:
    router = Router()
    admin_ids = _parse_admin_ids()
    env_path = os.getenv("WHITELIST_JSON")
    backup_path = env_path if env_path else "data/whitelist.json"
    tech_admin_ids_env = os.getenv("TECH_ADMIN_IDS", "")
    tech_admin_ids: list[int] = []
    for part in tech_admin_ids_env.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            tech_admin_ids.append(int(part))
        except ValueError:
            continue

    log = logging.getLogger(__name__)

    def _ensure_backup_dir() -> None:
        try:
            os.makedirs(os.path.dirname(backup_path) or ".", exist_ok=True)
        except Exception:
            log.warning("whitelist.backup.mkdir_failed path=%s", backup_path)

    def _load_backup_ids() -> list[str]:
        try:
            if not os.path.exists(backup_path):
                return []
            with open(backup_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            out: list[str] = []
            for v in raw if isinstance(raw, list) else []:
                try:
                    out.append(str(int(v)))
                except Exception:
                    continue
            return out
        except Exception as exc:
            log.error("whitelist.backup.read_failed path=%s err=%r", backup_path, exc)
            return []

    async def _write_backup(bot=None, add_id: int | None = None, remove_id: int | None = None) -> None:
        try:
            # Source 1: current Redis state (may be empty during outages)
            ids_redis_raw = await redis.smembers(WHITELIST_SET_KEY)
            ids_redis: set[int] = set()
            for v in ids_redis_raw:
                try:
                    ids_redis.add(int(v))
                except Exception:
                    try:
                        ids_redis.add(int(str(v)))
                    except Exception:
                        continue

            # Source 2: existing JSON file (guards against accidental shrink)
            ids_file: set[int] = set()
            try:
                if os.path.exists(backup_path):
                    with open(backup_path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    for v in raw if isinstance(raw, list) else []:
                        try:
                            ids_file.add(int(v))
                        except Exception:
                            continue
            except Exception as exc:
                log.error("whitelist.backup.read_failed path=%s err=%r", backup_path, exc)

            # Start from union to avoid accidental data loss when Redis is empty or partial
            items_set: set[int] = set(ids_file) | set(ids_redis)
            # Apply operation hints to ensure correctness on targeted changes
            if add_id is not None:
                items_set.add(int(add_id))
            if remove_id is not None:
                items_set.discard(int(remove_id))

            _ensure_backup_dir()
            tmp = backup_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sorted(items_set), f, ensure_ascii=False, indent=0)
            os.replace(tmp, backup_path)
            log.info("whitelist.backup.written path=%s count=%d", backup_path, len(items_set))
        except Exception as exc:
            log.error("whitelist.backup.write_failed path=%s err=%r", backup_path, exc)
            if bot is not None:
                try:
                    await _notify(bot, f"❗ whitelist backup write failed: {exc}")
                except Exception:
                    pass

    async def _notify(bot, text: str) -> None:
        if not tech_admin_ids:
            return
        for admin_id in tech_admin_ids:
            try:
                await bot.send_message(admin_id, text)
            except Exception:
                continue

    def _is_admin(uid: int) -> bool:
        return uid in admin_ids

    async def _labels_for_user(bot, uid: int) -> tuple[str | None, str | None]:
        uname = await redis.get(f"id_to_username:{uid}")
        full = await redis.get(f"id_to_fullname:{uid}")
        if uname and full:
            return uname, full
        # Fallback: query Telegram for latest username/fullname and cache them
        try:
            chat = await bot.get_chat(uid)
            if not uname and getattr(chat, "username", None):
                uname = "@" + chat.username.lower()
                try:
                    await redis.set(f"id_to_username:{uid}", uname, ex=30 * 24 * 3600)
                except Exception:
                    pass
            if not full and getattr(chat, "full_name", None):
                full = chat.full_name  # type: ignore[attr-defined]
                try:
                    await redis.set(f"id_to_fullname:{uid}", full, ex=30 * 24 * 3600)
                except Exception:
                    pass
        except Exception:
            pass
        return uname, full

    @router.message(Command("admin"))
    async def admin_help(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id):
            return
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="👥 Дозволені", callback_data="admin:allowed"
                    ),
                    InlineKeyboardButton(
                        text="📊 Статистика", callback_data="admin:stats"
                    ),
                ]
            ]
        )
        await message.answer(
            "Адмін команди:\n"
            "<code>/allow id</code> — додати за числовим ID\n"
            "<code>/allow_username @нік</code> — додати за нікнеймом\n"
            "/allow_from_forward — перешліть повідомлення користувача\n"
            "<code>/deny id</code> — видалити з білого списку\n"
            "/allowed — показати дозволених користувачів\n"
            "/stats — показати статистику\n"
            "<code>/setname id Повне Імʼя</code> — вручну змінити імʼя\n"
            "<code>/setusername id @нік</code> — вручну змінити нік",
            reply_markup=kb,
        )

    @router.message(Command("allow"))
    async def allow(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id):
            return
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Формат: /allow id або @нік")
            return

        arg = parts[1]
        uid: int | None = None
        if arg.startswith("@"):
            key = f"username_to_id:{arg.lower()}"
            val = await redis.get(key)
            if val and val.isdigit():
                uid = int(val)
            else:
                await message.answer(
                    "Немає відповідності для цього ніку. Використайте /allow_from_forward та перешліть повідомлення користувача."
                )
                return
        else:
            try:
                uid = int(arg)
            except ValueError:
                await message.answer("Невірний id")
                return

        await redis.sadd(WHITELIST_SET_KEY, str(uid))
        # Try to resolve labels right away for nicer /allowed output
        await _labels_for_user(message.bot, uid)
        await message.answer(f"Додано {uid} до білого списку")
        log.info("admin.allow actor_id=%s target_id=%s", message.from_user.id, uid)
        await _write_backup(message.bot, add_id=uid)
        await _notify(message.bot, f"✅ Allow: {uid}")

    @router.message(Command("deny"))
    async def deny(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id):
            return
        parts = (message.text or "").split()
        if len(parts) != 2:
            await message.answer("Формат: /deny <id>")
            return
        try:
            uid = int(parts[1])
        except ValueError:
            await message.answer("Невірний id")
            return
        await redis.srem(WHITELIST_SET_KEY, str(uid))
        await message.answer(f"Видалено {uid} з білого списку")
        log.info("admin.deny actor_id=%s target_id=%s", message.from_user.id, uid)
        await _write_backup(message.bot, remove_id=uid)
        await _notify(message.bot, f"⛔ Deny: {uid}")

    @router.message(Command("allow_username"))
    async def allow_username(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id):
            return
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].startswith("@"):
            await message.answer("Формат: /allow_username @нік")
            return
        uname = parts[1].lower()
        # If the mapping already exists, add immediately
        existing = await redis.get(f"username_to_id:{uname}")
        if existing and existing.isdigit():
            uid = int(existing)
            await redis.sadd(WHITELIST_SET_KEY, str(uid))
            await redis.set(f"id_to_username:{uid}", uname, ex=30 * 24 * 3600)
            await message.answer(f"Додано {uid} до білого списку")
            log.info("admin.allow_username actor_id=%s target_id=%s username=%s", message.from_user.id, uid, uname)
            await _write_backup(message.bot, add_id=uid)
            await _notify(message.bot, f"✅ Allow by username {uname} → {uid}")
            return
        # Try to resolve via Bot API if the user has interacted before
        try:
            chat = await message.bot.get_chat(uname)
            if chat and getattr(chat, "id", None):
                uid = int(chat.id)
                await redis.sadd(WHITELIST_SET_KEY, str(uid))
                await redis.set(f"username_to_id:{uname}", str(uid), ex=30 * 24 * 3600)
                await redis.set(f"id_to_username:{uid}", uname, ex=30 * 24 * 3600)
                await message.answer(f"Додано {uid} до білого списку (через Bot API)")
                log.info("admin.allow_username_api actor_id=%s target_id=%s username=%s", message.from_user.id, uid, uname)
                await _write_backup(message.bot, add_id=uid)
                await _notify(message.bot, f"✅ Allow by username {uname} → {uid} (API)")
                return
        except Exception:
            pass
        # Otherwise, store the nickname and request a forward or /start from the user
        await redis.set(f"username_to_id:{uname}", "", ex=30 * 24 * 3600)
        await message.answer(
            f"Нік {uname} збережено, але користувача ще не додано.\n"
            "Використайте /allow_from_forward і перешліть повідомлення користувача,\n"
            "або попросіть його написати боту /start."
        )

    @router.message(Command("allow_from_forward"))
    async def allow_from_forward(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id):
            return
        admin_id = message.from_user.id
        await redis.set(f"admin:await_forward:{admin_id}", "1", ex=300)
        await message.answer("Перешліть повідомлення від користувача, щоб додати його")

    @router.message(
        lambda m: (m.forward_from is not None)
        or (getattr(m, "forward_origin", None) is not None)
    )
    async def handle_forward_allow(message: Message) -> None:
        # Only proceed if admin is awaiting a forward
        if not message.from_user or not _is_admin(message.from_user.id):
            return
        pending = await redis.get(f"admin:await_forward:{message.from_user.id}")
        if not pending:
            return

        uid: int | None = None
        uname: str | None = None

        if message.forward_from:
            uid = message.forward_from.id
            if message.forward_from.username:
                uname = "@" + message.forward_from.username.lower()
            # Full name if available
            full_name = getattr(message.forward_from, "full_name", None)
        elif getattr(message, "forward_origin", None):
            origin = message.forward_origin
            sender = getattr(origin, "sender_user", None)
            if sender is not None:
                uid = sender.id
                if getattr(sender, "username", None):
                    uname = "@" + sender.username.lower()
                full_name = getattr(sender, "full_name", None)

        if uid is None:
            await message.answer(
                "Не вдалося отримати ID з пересланого повідомлення (можливо, налаштування приватності).\n"
                "Попросіть користувача написати боту /start — тоді можна буде додати його."
            )
            return

        await redis.delete(f"admin:await_forward:{message.from_user.id}")
        await redis.sadd(WHITELIST_SET_KEY, str(uid))
        if uname:
            await redis.set(f"username_to_id:{uname}", str(uid), ex=30 * 24 * 3600)
            await redis.set(f"id_to_username:{uid}", uname, ex=30 * 24 * 3600)
        if "full_name" in locals() and full_name:
            await redis.set(f"id_to_fullname:{uid}", full_name, ex=30 * 24 * 3600)
        await message.answer(f"Додано {uid} до білого списку")
        log.info("admin.allow_from_forward actor_id=%s target_id=%s", message.from_user.id, uid)
        await _write_backup(message.bot, add_id=uid)
        await _notify(message.bot, f"✅ Allow from forward → {uid}")

    @router.message(Command("allowed"))
    async def allowed(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id):
            return
        ids = await redis.smembers(WHITELIST_SET_KEY)
        if not ids:
            # Fallback to JSON backup to display something useful
            ids = set(_load_backup_ids())
        rows: list[tuple[str, str | None, str | None]] = []
        for uid in sorted(ids):
            try:
                uid_int = int(uid)
            except Exception:
                uid_int = None  # type: ignore[assignment]
            if uid_int is not None:
                uname, full = await _labels_for_user(message.bot, uid_int)
            else:
                uname = await redis.get(f"id_to_username:{uid}")
                full = await redis.get(f"id_to_fullname:{uid}")
            rows.append((uid, uname, full))
        await message.answer(render_allowed_users(rows))

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id):
            return
        daily = await stats_service.get_daily(redis)
        weekly = await stats_service.get_weekly(redis)
        # Map user_id to username if known
        usernames: dict[str, str] = {}
        for uid in set(list(daily.keys()) + list(weekly.keys())):
            name = await redis.get(f"id_to_username:{uid}")
            if name:
                usernames[uid] = name
        daily_rows = stats_service.rank(daily, usernames)
        weekly_rows = stats_service.rank(weekly, usernames)
        await message.answer(render_usage_tables(daily_rows, weekly_rows))

    @router.callback_query(lambda c: c.data == "admin:allowed")
    async def cb_allowed(callback: CallbackQuery) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id):
            return
        ids = await redis.smembers(WHITELIST_SET_KEY)
        if not ids:
            ids = set(_load_backup_ids())
        rows: list[tuple[str, str | None, str | None]] = []
        for uid in sorted(ids):
            try:
                uid_int = int(uid)
            except Exception:
                uid_int = None  # type: ignore[assignment]
            if uid_int is not None:
                uname, full = await _labels_for_user(callback.bot, uid_int)
            else:
                uname = await redis.get(f"id_to_username:{uid}")
                full = await redis.get(f"id_to_fullname:{uid}")
            rows.append((uid, uname, full))
        await callback.message.edit_text(render_allowed_users(rows))
        await callback.answer()

    @router.callback_query(lambda c: c.data == "admin:stats")
    async def cb_stats(callback: CallbackQuery) -> None:
        if not callback.from_user or not _is_admin(callback.from_user.id):
            return
        daily = await stats_service.get_daily(redis)
        weekly = await stats_service.get_weekly(redis)
        usernames: dict[str, str] = {}
        for uid in set(list(daily.keys()) + list(weekly.keys())):
            name = await redis.get(f"id_to_username:{uid}")
            if name:
                usernames[uid] = name
        daily_rows = stats_service.rank(daily, usernames)
        weekly_rows = stats_service.rank(weekly, usernames)
        await callback.message.edit_text(render_usage_tables(daily_rows, weekly_rows))
        await callback.answer()

    @router.message(Command("setname"))
    async def setname(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id):
            return
        # /setname <id> <full name>
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3:
            await message.answer("Формат: /setname id Повне Імʼя")
            return
        try:
            uid = int(parts[1])
        except ValueError:
            await message.answer("Невірний id")
            return
        full_name = parts[2].strip()
        await redis.set(f"id_to_fullname:{uid}", full_name, ex=30 * 24 * 3600)
        await message.answer("Імʼя оновлено")

    @router.message(Command("setusername"))
    async def setusername(message: Message) -> None:
        if not message.from_user or not _is_admin(message.from_user.id):
            return
        # /setusername <id> @username
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3 or not parts[2].startswith("@"):
            await message.answer("Формат: /setusername id @нік")
            return
        try:
            uid = int(parts[1])
        except ValueError:
            await message.answer("Невірний id")
            return
        uname = parts[2].lower()
        await redis.set(f"id_to_username:{uid}", uname, ex=30 * 24 * 3600)
        await redis.set(f"username_to_id:{uname}", str(uid), ex=30 * 24 * 3600)
        await message.answer("Нік оновлено")

    return router

