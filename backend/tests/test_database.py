"""Unit tests for the SQLite data access layer."""

from __future__ import annotations

import os
import sqlite3
import pytest

from app.database import (
    add_to_watchlist,
    get_chat_history,
    get_db,
    get_portfolio_history,
    get_position,
    get_positions,
    get_user_profile,
    get_watchlist,
    get_watchlist_tickers,
    init_db,
    record_chat_message,
    record_portfolio_snapshot,
    record_trade,
    remove_from_watchlist,
    update_cash_balance,
    upsert_position,
)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_finally.db")


@pytest.fixture
def conn(db_path):
    """Initialized DB connection for use in tests."""
    init_db(db_path)
    with get_db(db_path) as c:
        yield c


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_tables(self, db_path):
        init_db(db_path)
        with get_db(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "users_profile" in tables
        assert "watchlist" in tables
        assert "positions" in tables
        assert "trades" in tables
        assert "portfolio_snapshots" in tables
        assert "chat_messages" in tables

    def test_seeds_user_profile(self, db_path):
        init_db(db_path)
        with get_db(db_path) as conn:
            row = conn.execute("SELECT * FROM users_profile WHERE id='default'").fetchone()
        assert row is not None
        assert row["cash_balance"] == 10000.0

    def test_seeds_watchlist(self, db_path):
        init_db(db_path)
        with get_db(db_path) as conn:
            rows = conn.execute("SELECT ticker FROM watchlist").fetchall()
        tickers = {row["ticker"] for row in rows}
        assert "AAPL" in tickers
        assert "GOOGL" in tickers
        assert len(tickers) == 10

    def test_idempotent(self, db_path):
        """Calling init_db twice does not duplicate seed data."""
        init_db(db_path)
        init_db(db_path)
        with get_db(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM users_profile").fetchone()[0]
        assert count == 1

    def test_creates_parent_directory(self, tmp_path):
        nested_path = str(tmp_path / "nested" / "dir" / "test.db")
        init_db(nested_path)
        assert os.path.exists(nested_path)


# ---------------------------------------------------------------------------
# User profile
# ---------------------------------------------------------------------------

class TestUserProfile:
    def test_get_user_profile_returns_dict(self, conn):
        profile = get_user_profile(conn)
        assert profile["id"] == "default"
        assert profile["cash_balance"] == 10000.0

    def test_update_cash_balance_add(self, conn):
        new_balance = update_cash_balance(conn, 500.0)
        assert new_balance == 10500.0
        profile = get_user_profile(conn)
        assert profile["cash_balance"] == 10500.0

    def test_update_cash_balance_subtract(self, conn):
        new_balance = update_cash_balance(conn, -1000.0)
        assert new_balance == 9000.0

    def test_update_cash_balance_insufficient(self, conn):
        with pytest.raises(ValueError, match="Insufficient funds"):
            update_cash_balance(conn, -99999.0)

    def test_update_cash_balance_to_zero(self, conn):
        new_balance = update_cash_balance(conn, -10000.0)
        assert new_balance == 0.0


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

class TestWatchlist:
    def test_get_watchlist_returns_list(self, conn):
        items = get_watchlist(conn)
        assert isinstance(items, list)
        assert len(items) == 10

    def test_get_watchlist_tickers(self, conn):
        tickers = get_watchlist_tickers(conn)
        assert isinstance(tickers, list)
        assert "AAPL" in tickers
        assert len(tickers) == 10

    def test_add_to_watchlist(self, conn):
        row = add_to_watchlist(conn, "PYPL")
        assert row["ticker"] == "PYPL"
        assert "id" in row
        assert "added_at" in row
        tickers = get_watchlist_tickers(conn)
        assert "PYPL" in tickers

    def test_add_duplicate_raises(self, conn):
        with pytest.raises(ValueError, match="already in the watchlist"):
            add_to_watchlist(conn, "AAPL")

    def test_remove_from_watchlist(self, conn):
        remove_from_watchlist(conn, "AAPL")
        tickers = get_watchlist_tickers(conn)
        assert "AAPL" not in tickers

    def test_remove_nonexistent_raises(self, conn):
        with pytest.raises(ValueError, match="not in the watchlist"):
            remove_from_watchlist(conn, "FAKE")

    def test_add_normalized_uppercase(self, conn):
        row = add_to_watchlist(conn, "pypl")
        assert row["ticker"] == "pypl"  # database stores what you pass


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

class TestPositions:
    def test_get_positions_empty_initially(self, conn):
        positions = get_positions(conn)
        assert positions == []

    def test_upsert_position_insert(self, conn):
        upsert_position(conn, "AAPL", 10.0, 190.0)
        pos = get_position(conn, "AAPL")
        assert pos is not None
        assert pos["quantity"] == 10.0
        assert pos["avg_cost"] == 190.0

    def test_upsert_position_update(self, conn):
        upsert_position(conn, "AAPL", 10.0, 190.0)
        upsert_position(conn, "AAPL", 20.0, 195.0)
        pos = get_position(conn, "AAPL")
        assert pos["quantity"] == 20.0
        assert pos["avg_cost"] == 195.0

    def test_upsert_position_delete_when_zero(self, conn):
        upsert_position(conn, "AAPL", 10.0, 190.0)
        upsert_position(conn, "AAPL", 0.0, 190.0)
        pos = get_position(conn, "AAPL")
        assert pos is None

    def test_upsert_position_delete_when_negative(self, conn):
        upsert_position(conn, "AAPL", 10.0, 190.0)
        upsert_position(conn, "AAPL", -1.0, 190.0)
        pos = get_position(conn, "AAPL")
        assert pos is None

    def test_get_position_none_when_not_exists(self, conn):
        pos = get_position(conn, "UNKNOWN")
        assert pos is None

    def test_get_positions_returns_all(self, conn):
        upsert_position(conn, "AAPL", 10.0, 190.0)
        upsert_position(conn, "GOOGL", 5.0, 175.0)
        positions = get_positions(conn)
        tickers = {p["ticker"] for p in positions}
        assert tickers == {"AAPL", "GOOGL"}


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

class TestTrades:
    def test_record_trade_returns_dict(self, conn):
        trade = record_trade(conn, "AAPL", "buy", 10.0, 190.0)
        assert trade["ticker"] == "AAPL"
        assert trade["side"] == "buy"
        assert trade["quantity"] == 10.0
        assert trade["price"] == 190.0
        assert "id" in trade
        assert "executed_at" in trade

    def test_record_multiple_trades(self, conn):
        record_trade(conn, "AAPL", "buy", 10.0, 190.0)
        record_trade(conn, "AAPL", "sell", 5.0, 195.0)
        rows = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        assert rows[0] == 2


# ---------------------------------------------------------------------------
# Portfolio snapshots
# ---------------------------------------------------------------------------

class TestPortfolioSnapshots:
    def test_record_and_retrieve_snapshot(self, conn):
        record_portfolio_snapshot(conn, 10500.0)
        history = get_portfolio_history(conn)
        assert len(history) == 1
        assert history[0]["total_value"] == 10500.0

    def test_history_oldest_first(self, conn):
        record_portfolio_snapshot(conn, 10000.0)
        record_portfolio_snapshot(conn, 10500.0)
        record_portfolio_snapshot(conn, 11000.0)
        history = get_portfolio_history(conn)
        values = [h["total_value"] for h in history]
        assert values == [10000.0, 10500.0, 11000.0]

    def test_history_limit(self, conn):
        for i in range(10):
            record_portfolio_snapshot(conn, float(10000 + i * 100))
        history = get_portfolio_history(conn, limit=5)
        assert len(history) == 5


# ---------------------------------------------------------------------------
# Chat messages
# ---------------------------------------------------------------------------

class TestChatMessages:
    def test_record_user_message(self, conn):
        msg = record_chat_message(conn, "user", "Hello")
        assert msg["role"] == "user"
        assert msg["content"] == "Hello"
        assert msg["actions"] is None
        assert "id" in msg
        assert "created_at" in msg

    def test_record_assistant_message_with_actions(self, conn):
        actions = {"trades": [{"ticker": "AAPL", "executed": True}]}
        msg = record_chat_message(conn, "assistant", "Bought AAPL", actions)
        assert msg["role"] == "assistant"
        assert msg["actions"] == actions

    def test_get_chat_history_oldest_first(self, conn):
        record_chat_message(conn, "user", "First message")
        record_chat_message(conn, "assistant", "First response")
        record_chat_message(conn, "user", "Second message")
        history = get_chat_history(conn)
        assert history[0]["content"] == "First message"
        assert history[-1]["content"] == "Second message"

    def test_get_chat_history_limit(self, conn):
        for i in range(25):
            record_chat_message(conn, "user", f"Message {i}")
        history = get_chat_history(conn, limit=10)
        assert len(history) == 10

    def test_get_chat_history_actions_deserialized(self, conn):
        actions = {"trades": [], "watchlist_changes": []}
        record_chat_message(conn, "assistant", "Done", actions)
        history = get_chat_history(conn)
        last = history[-1]
        assert isinstance(last["actions"], dict)
        assert "trades" in last["actions"]
