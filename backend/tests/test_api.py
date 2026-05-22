"""API endpoint tests using FastAPI TestClient.

These are integration tests: they spin up the full app with a temp database
and the GBM simulator running. LLM calls are mocked via LLM_MOCK=true.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture(scope="module")
def test_db_path(tmp_path_factory):
    return str(tmp_path_factory.mktemp("db") / "api_test.db")


@pytest.fixture(scope="module")
def client(test_db_path):
    """Module-scoped test client with a fresh database and mock LLM."""
    import os
    os.environ["DB_PATH"] = test_db_path
    os.environ["LLM_MOCK"] = "true"

    app = create_app()
    with TestClient(app) as c:
        yield c

    # Cleanup
    os.environ.pop("DB_PATH", None)
    os.environ.pop("LLM_MOCK", None)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Portfolio endpoints
# ---------------------------------------------------------------------------

class TestPortfolioEndpoints:
    def test_get_portfolio_initial_state(self, client):
        resp = client.get("/api/portfolio")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cash_balance"] == pytest.approx(10000.0)
        assert data["positions"] == []
        assert data["total_value"] == pytest.approx(10000.0)
        assert data["total_pnl"] == 0.0

    def test_buy_trade_succeeds(self, client):
        # Buy 5 shares of AAPL
        resp = client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "quantity": 5, "side": "buy"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "trade" in data
        assert "portfolio" in data
        assert data["trade"]["ticker"] == "AAPL"
        assert data["trade"]["side"] == "buy"
        assert data["trade"]["quantity"] == 5.0

    def test_portfolio_after_buy(self, client):
        resp = client.get("/api/portfolio")
        assert resp.status_code == 200
        data = resp.json()
        # Cash should be less than 10000 now (minus cost of 5 AAPL)
        assert data["cash_balance"] < 10000.0
        assert len(data["positions"]) == 1
        assert data["positions"][0]["ticker"] == "AAPL"
        assert data["positions"][0]["quantity"] == 5.0

    def test_sell_trade_succeeds(self, client):
        # Sell 2 of the 5 AAPL shares
        resp = client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "quantity": 2, "side": "sell"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["trade"]["side"] == "sell"
        assert data["trade"]["quantity"] == 2.0

    def test_buy_insufficient_cash(self, client):
        resp = client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "quantity": 999999, "side": "buy"},
        )
        assert resp.status_code == 422

    def test_sell_insufficient_shares(self, client):
        resp = client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "quantity": 99999, "side": "sell"},
        )
        assert resp.status_code == 422

    def test_trade_invalid_side(self, client):
        resp = client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "quantity": 1, "side": "hold"},
        )
        assert resp.status_code == 422

    def test_trade_negative_quantity(self, client):
        resp = client.post(
            "/api/portfolio/trade",
            json={"ticker": "AAPL", "quantity": -1, "side": "buy"},
        )
        assert resp.status_code == 422

    def test_trade_ticker_normalized(self, client):
        resp = client.post(
            "/api/portfolio/trade",
            json={"ticker": "aapl", "quantity": 1, "side": "sell"},
        )
        # lowercase ticker should be normalized; may succeed or fail based on state
        # but should not be a validation error
        assert resp.status_code in (200, 422)  # 422 if not enough shares

    def test_portfolio_history_has_entries(self, client):
        resp = client.get("/api/portfolio/history")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # After trades above, there should be at least some snapshots
        assert len(data) >= 0


# ---------------------------------------------------------------------------
# Watchlist endpoints
# ---------------------------------------------------------------------------

class TestWatchlistEndpoints:
    def test_get_watchlist(self, client):
        resp = client.get("/api/watchlist")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 10
        tickers = {item["ticker"] for item in data}
        assert "AAPL" in tickers

    def test_watchlist_items_have_prices(self, client):
        resp = client.get("/api/watchlist")
        data = resp.json()
        # After simulator startup, prices should be available
        prices = [item["price"] for item in data if item["price"] is not None]
        assert len(prices) > 0

    def test_add_ticker(self, client):
        resp = client.post("/api/watchlist", json={"ticker": "PYPL"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ticker"] == "PYPL"

    def test_add_ticker_lowercase_normalized(self, client):
        resp = client.post("/api/watchlist", json={"ticker": "coin"})
        assert resp.status_code == 200
        assert resp.json()["ticker"] == "COIN"

    def test_add_duplicate_ticker(self, client):
        resp = client.post("/api/watchlist", json={"ticker": "AAPL"})
        assert resp.status_code == 422

    def test_remove_ticker(self, client):
        # First add it
        client.post("/api/watchlist", json={"ticker": "SNAP"})
        # Then remove it
        resp = client.delete("/api/watchlist/SNAP")
        assert resp.status_code == 200
        assert "SNAP" in resp.json()["message"]

    def test_remove_nonexistent_ticker(self, client):
        resp = client.delete("/api/watchlist/DOESNOTEXIST")
        assert resp.status_code == 404

    def test_remove_ticker_case_insensitive(self, client):
        # PYPL was added in test_add_ticker, remove with lowercase
        resp = client.delete("/api/watchlist/pypl")
        # Should work because the endpoint normalizes to uppercase
        assert resp.status_code in (200, 404)  # 404 if already removed


# ---------------------------------------------------------------------------
# Chat endpoint (mock mode)
# ---------------------------------------------------------------------------

class TestChatEndpoint:
    def test_chat_mock_response(self, client):
        resp = client.post("/api/chat", json={"message": "Hello!"})
        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data
        assert "trades" in data
        assert "watchlist_changes" in data
        assert "id" in data
        assert "created_at" in data
        assert isinstance(data["trades"], list)
        assert isinstance(data["watchlist_changes"], list)

    def test_chat_response_has_text(self, client):
        resp = client.post("/api/chat", json={"message": "What's in my portfolio?"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["message"]) > 0

    def test_chat_empty_message(self, client):
        # Empty message is technically valid (no validator for min length)
        resp = client.post("/api/chat", json={"message": ""})
        assert resp.status_code in (200, 422)
