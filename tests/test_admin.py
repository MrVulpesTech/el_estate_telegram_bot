"""
File created: Tests for admin stats rendering utilities.
"""

import pytest

from src.bot.admin.table_render import render_usage_tables


def test_render_usage_tables_formats_columns():
    daily = [("@alice", 12), ("@bob", 7), ("123456789", 3)]
    weekly = [("@alice", 54), ("@bob", 39), ("123456789", 11)]
    text = render_usage_tables(daily, weekly)
    assert "Usage — Last 24h" in text
    assert "Usage — Last 7d" in text
    assert "@alice" in text and "@bob" in text
    # Ensure counts are present and right-aligned area exists (simple check)
    assert "  12" in text
    assert "  54" in text


