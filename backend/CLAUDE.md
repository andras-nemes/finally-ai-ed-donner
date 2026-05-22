# Backend — Developer Guide

## Project Setup

```bash
cd backend
uv sync --extra dev   # Install all dependencies including test/lint tools
```

## Running the Server

```bash
uv run uvicorn app.main:app --reload --port 8000
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | Yes (for chat) | — | OpenAI / OpenRouter API key |
| `OPENAI_BASE_URL` | No | OpenAI default | Override for OpenRouter: `https://openrouter.ai/api/v1` |
| `DB_PATH` | No | `../../db/finally.db` | SQLite database file path |
| `MASSIVE_API_KEY` | No | — | Polygon.io API key (uses GBM simulator if unset) |
| `LLM_MOCK` | No | `false` | Set `true` for deterministic mock LLM responses |

## API Endpoints

### System
- `GET /api/health` — `{"status": "ok"}`

### Market Data
- `GET /api/stream/prices` — SSE stream; events contain all ticker prices every ~500ms

### Portfolio
- `GET /api/portfolio` — positions with live P&L, cash balance, totals
- `POST /api/portfolio/trade` — `{ticker, quantity, side: "buy"|"sell"}`; market order
- `GET /api/portfolio/history` — portfolio value snapshots (up to 200, oldest-first)

### Watchlist
- `GET /api/watchlist` — all tickers with live prices
- `POST /api/watchlist` — `{ticker}` — add a ticker
- `DELETE /api/watchlist/{ticker}` — remove a ticker

### Chat
- `POST /api/chat` — `{message}` — send message to AI assistant; returns structured response with auto-executed trades and watchlist changes

## Market Data API

The market data subsystem lives in `app/market/`. Use these imports:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source
```

### Core Types

- **`PriceUpdate`** — Immutable dataclass: `ticker`, `price`, `previous_price`, `timestamp`, plus properties `change`, `change_percent`, `direction` ("up"/"down"/"flat"), and `to_dict()` for JSON serialization.

- **`PriceCache`** — Thread-safe in-memory store. Key methods:
  - `update(ticker, price, timestamp=None) -> PriceUpdate`
  - `get(ticker) -> PriceUpdate | None`
  - `get_price(ticker) -> float | None`
  - `get_all() -> dict[str, PriceUpdate]`
  - `remove(ticker)`
  - `version` property — monotonic counter, increments on every update (for SSE change detection)

- **`MarketDataSource`** — Abstract interface implemented by `SimulatorDataSource` and `MassiveDataSource`. Lifecycle: `start(tickers)` -> `add_ticker()` / `remove_ticker()` -> `stop()`.

- **`create_market_data_source(cache)`** — Factory. Returns `MassiveDataSource` if `MASSIVE_API_KEY` is set, otherwise `SimulatorDataSource`.

### SSE Streaming

The SSE endpoint `GET /api/stream/prices` is implemented inline in `app/main.py`. It streams all ticker prices every ~500ms using the shared `PriceCache` from `app.state`.

### Seed Data

Default tickers: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX. Seed prices and per-ticker volatility/drift params are in `app/market/seed_prices.py`.

## Running Tests

```bash
uv run --extra dev pytest -v              # All tests
uv run --extra dev pytest --cov=app       # With coverage
uv run --extra dev ruff check app/ tests/ # Lint
```

## Demo

```bash
uv run market_data_demo.py   # Live terminal dashboard with simulated prices
```
