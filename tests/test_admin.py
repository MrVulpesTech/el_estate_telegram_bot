"""
Tests for admin stats rendering utilities.
"""

from src.bot.admin.table_render import render_usage_tables


def test_render_usage_tables_formats_columns():
    daily = [("@alice", 12), ("@bob", 7), ("123456789", 3)]
    weekly = [("@alice", 54), ("@bob", 39), ("123456789", 11)]
    text = render_usage_tables(daily, weekly)
    assert "Статистика — Останні 24 години" in text
    assert "Статистика — Останні 7 днів" in text
    assert "@alice" in text and "@bob" in text
    assert "  12" in text
    assert "  54" in text
