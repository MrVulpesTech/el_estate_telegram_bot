"""
File created: Stats service for daily/weekly counters with TTLs and queries.
Provides increment and read helpers backed by Redis.
"""

import datetime as _dt
from typing import Dict, List, Tuple

import redis.asyncio as aioredis


DAILY_TTL_DAYS = 35
WEEKLY_TTL_DAYS = 180


def _today_key() -> str:
    today = _dt.date.today()
    return f"stats:daily:{today.isoformat()}"


def _week_key() -> str:
    today = _dt.date.today()
    year, week, _ = today.isocalendar()
    return f"stats:weekly:{year}-{week:02d}"


async def increment(redis: aioredis.Redis, user_id: int) -> None:
    daily_key = _today_key()
    weekly_key = _week_key()

    # Daily
    await redis.hincrby(daily_key, str(user_id), 1)
    await redis.expire(daily_key, DAILY_TTL_DAYS * 24 * 3600)

    # Weekly
    await redis.hincrby(weekly_key, str(user_id), 1)
    await redis.expire(weekly_key, WEEKLY_TTL_DAYS * 24 * 3600)


async def get_daily(redis: aioredis.Redis) -> Dict[str, int]:
    key = _today_key()
    data = await redis.hgetall(key)
    return {k: int(v) for k, v in data.items()} if data else {}


async def get_weekly(redis: aioredis.Redis) -> Dict[str, int]:
    key = _week_key()
    data = await redis.hgetall(key)
    return {k: int(v) for k, v in data.items()} if data else {}


def rank(items: Dict[str, int], usernames: Dict[str, str] | None = None) -> List[Tuple[str, int]]:
    # Returns list of (display_name, count) sorted desc by count
    pairs = list(items.items())
    pairs.sort(key=lambda kv: kv[1], reverse=True)
    out: List[Tuple[str, int]] = []
    for user_id, count in pairs:
        label = usernames.get(user_id, user_id) if usernames else user_id
        out.append((label, count))
    return out


