"""
Render monospaced tables for allowed users and usage stats.
"""

from typing import Iterable, Tuple


def render_usage_tables(
    daily_rows: Iterable[Tuple[str, int]],
    weekly_rows: Iterable[Tuple[str, int]],
) -> str:
    def _render_block(title: str, rows: Iterable[Tuple[str, int]]) -> str:
        rows = list(rows)
        name_width = max([len(r[0]) for r in rows] + [4, 16])
        lines = [title, f"#  {'користувач'.ljust(name_width)}  кількість"]
        for i, (name, count) in enumerate(rows, start=1):
            lines.append(f"{i:<2} {name.ljust(name_width)}  {count:>5}")
        return "\n".join(lines)

    blocks = [
        _render_block("📊 Статистика — Останні 24 години", daily_rows),
        _render_block("\n📈 Статистика — Останні 7 днів", weekly_rows),
    ]
    return "\n".join(blocks)


def render_allowed_users(rows: Iterable[Tuple[str, str | None, str | None]]) -> str:
    # rows: iterable of (user_id_str, username or None, full_name or None)
    rows = list(rows)
    id_width = max([len(r[0]) for r in rows] + [8])
    nick_width = max([len((r[1] or "-")) for r in rows] + [8, 4])
    full_width = max([len((r[2] or "-")) for r in rows] + [8, 4])
    lines = [
        "Дозволені користувачі",
        f"#  {'id'.ljust(id_width)}  {'нік'.ljust(nick_width)}  {'імʼя'.ljust(full_width)}",
    ]
    for i, (uid, uname, full_name) in enumerate(rows, start=1):
        uname = uname or "-"
        full_name = full_name or "-"
        id_cell = f"<code>{uid}</code>"
        lines.append(f"{i:<2} {id_cell}  {uname.ljust(nick_width)}  {full_name.ljust(full_width)}")
    return "\n".join(lines)


