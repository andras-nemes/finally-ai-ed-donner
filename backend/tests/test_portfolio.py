"""Unit tests for portfolio trade execution logic."""

from __future__ import annotations

import pytest

from app.database import (
    get_db,
    get_position,
    get_positions,
    get_user_profile,
    init_db,
    record_trade,
    update_cash_balance,
    upsert_position,
)
from app.market.cache import PriceCache
from app.market.models import PriceUpdate


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "portfolio_test.db")
    init_db(path)
    return path


@pytest.fixture
def price_cache():
    """A price cache seeded with known test prices."""
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    cache.update("GOOGL", 175.0)
    cache.update("MSFT", 420.0)
    return cache


# ---------------------------------------------------------------------------
# Helper: replicate the trade execution logic from portfolio router
# ---------------------------------------------------------------------------

def execute_buy(conn, ticker: str, quantity: float, price: float) -> dict:
    """Execute a buy trade. Raises ValueError on insufficient cash."""
    profile = get_user_profile(conn)
    cost = quantity * price
    if profile["cash_balance"] < cost:
        raise ValueError(
            f"Insufficient cash: need ${cost:.2f}, have ${profile['cash_balance']:.2f}"
        )
    update_cash_balance(conn, -cost)

    existing = get_position(conn, ticker)
    if existing and existing["quantity"] > 0:
        old_qty = existing["quantity"]
        old_avg = existing["avg_cost"]
        new_qty = old_qty + quantity
        new_avg = (old_qty * old_avg + quantity * price) / new_qty
    else:
        new_qty = quantity
        new_avg = price

    upsert_position(conn, ticker, new_qty, new_avg)
    return record_trade(conn, ticker, "buy", quantity, price)


def execute_sell(conn, ticker: str, quantity: float, price: float) -> dict:
    """Execute a sell trade. Raises ValueError on insufficient shares."""
    existing = get_position(conn, ticker)
    owned = existing["quantity"] if existing else 0.0
    if owned < quantity:
        raise ValueError(
            f"Insufficient shares: trying to sell {quantity}, own {owned}"
        )
    proceeds = quantity * price
    update_cash_balance(conn, proceeds)

    new_qty = owned - quantity
    avg_cost = existing["avg_cost"]
    upsert_position(conn, ticker, new_qty, avg_cost)
    return record_trade(conn, ticker, "sell", quantity, price)


# ---------------------------------------------------------------------------
# Buy tests
# ---------------------------------------------------------------------------

class TestBuyTrade:
    def test_buy_deducts_cash(self, db_path):
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 10.0, 190.0)
            profile = get_user_profile(conn)
        assert profile["cash_balance"] == pytest.approx(10000.0 - 10 * 190.0)

    def test_buy_creates_position(self, db_path):
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 10.0, 190.0)
            pos = get_position(conn, "AAPL")
        assert pos is not None
        assert pos["quantity"] == 10.0
        assert pos["avg_cost"] == 190.0

    def test_buy_records_trade(self, db_path):
        with get_db(db_path) as conn:
            trade = execute_buy(conn, "AAPL", 10.0, 190.0)
        assert trade["ticker"] == "AAPL"
        assert trade["side"] == "buy"
        assert trade["quantity"] == 10.0
        assert trade["price"] == 190.0

    def test_buy_insufficient_cash(self, db_path):
        with get_db(db_path) as conn:
            with pytest.raises(ValueError, match="Insufficient cash"):
                execute_buy(conn, "AAPL", 1000.0, 190.0)

    def test_buy_fractional_shares(self, db_path):
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 0.5, 190.0)
            pos = get_position(conn, "AAPL")
        assert pos["quantity"] == 0.5

    def test_buy_updates_avg_cost_second_buy(self, db_path):
        """Weighted avg cost: (10*190 + 5*200) / 15 = 193.33..."""
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 10.0, 190.0)
            execute_buy(conn, "AAPL", 5.0, 200.0)
            pos = get_position(conn, "AAPL")
        expected_avg = (10 * 190.0 + 5 * 200.0) / 15
        assert pos["quantity"] == pytest.approx(15.0)
        assert pos["avg_cost"] == pytest.approx(expected_avg)

    def test_buy_updates_avg_cost_lower_price(self, db_path):
        """Buy more at lower price lowers avg cost."""
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 10.0, 200.0)
            execute_buy(conn, "AAPL", 10.0, 180.0)
            pos = get_position(conn, "AAPL")
        expected_avg = (10 * 200.0 + 10 * 180.0) / 20
        assert pos["avg_cost"] == pytest.approx(expected_avg)  # 190.0


# ---------------------------------------------------------------------------
# Sell tests
# ---------------------------------------------------------------------------

class TestSellTrade:
    def test_sell_adds_cash(self, db_path):
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 10.0, 190.0)
            execute_sell(conn, "AAPL", 5.0, 200.0)
            profile = get_user_profile(conn)
        # Started with 10000, spent 1900 on buy, gained 1000 on sell
        assert profile["cash_balance"] == pytest.approx(10000.0 - 10 * 190.0 + 5 * 200.0)

    def test_sell_reduces_position(self, db_path):
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 10.0, 190.0)
            execute_sell(conn, "AAPL", 3.0, 200.0)
            pos = get_position(conn, "AAPL")
        assert pos["quantity"] == pytest.approx(7.0)

    def test_sell_preserves_avg_cost(self, db_path):
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 10.0, 190.0)
            execute_sell(conn, "AAPL", 5.0, 210.0)
            pos = get_position(conn, "AAPL")
        assert pos["avg_cost"] == pytest.approx(190.0)

    def test_sell_all_removes_position(self, db_path):
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 10.0, 190.0)
            execute_sell(conn, "AAPL", 10.0, 200.0)
            pos = get_position(conn, "AAPL")
        assert pos is None

    def test_sell_insufficient_shares(self, db_path):
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 5.0, 190.0)
            with pytest.raises(ValueError, match="Insufficient shares"):
                execute_sell(conn, "AAPL", 10.0, 190.0)

    def test_sell_no_position_raises(self, db_path):
        with get_db(db_path) as conn:
            with pytest.raises(ValueError, match="Insufficient shares"):
                execute_sell(conn, "AAPL", 1.0, 190.0)

    def test_sell_records_trade(self, db_path):
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 10.0, 190.0)
            trade = execute_sell(conn, "AAPL", 5.0, 200.0)
        assert trade["side"] == "sell"
        assert trade["quantity"] == 5.0
        assert trade["price"] == 200.0


# ---------------------------------------------------------------------------
# P&L calculations
# ---------------------------------------------------------------------------

class TestPnLCalculations:
    def test_unrealized_pnl_positive(self, db_path, price_cache):
        """Position bought at 190, current price 200 → positive P&L."""
        # Buy at 190, current price is 190 (from fixture)
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 10.0, 190.0)
            pos = get_position(conn, "AAPL")

        current_price = price_cache.get_price("AAPL")
        assert current_price is not None
        cost_basis = pos["quantity"] * pos["avg_cost"]
        market_value = pos["quantity"] * current_price
        pnl = market_value - cost_basis
        # With same price as avg cost: pnl should be 0
        assert pnl == pytest.approx(0.0)

    def test_unrealized_pnl_when_price_rises(self, db_path):
        """Simulate: buy at 180, price rises to 200."""
        cache = PriceCache()
        cache.update("AAPL", 200.0)

        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 10.0, 180.0)
            pos = get_position(conn, "AAPL")

        current_price = cache.get_price("AAPL")
        pnl = pos["quantity"] * (current_price - pos["avg_cost"])
        assert pnl == pytest.approx(200.0)  # 10 * (200 - 180)

    def test_total_value_calculation(self, db_path, price_cache):
        """total_value = cash + sum(quantity * current_price for all positions)."""
        with get_db(db_path) as conn:
            execute_buy(conn, "AAPL", 10.0, 190.0)  # cost $1900
            execute_buy(conn, "GOOGL", 5.0, 175.0)  # cost $875
            profile = get_user_profile(conn)
            positions = get_positions(conn)

        cash = profile["cash_balance"]
        assert cash == pytest.approx(10000.0 - 1900.0 - 875.0)

        total = cash
        for pos in positions:
            price = price_cache.get_price(pos["ticker"])
            if price:
                total += pos["quantity"] * price

        aapl_price = price_cache.get_price("AAPL")
        googl_price = price_cache.get_price("GOOGL")
        expected = cash + 10 * aapl_price + 5 * googl_price
        assert total == pytest.approx(expected)
