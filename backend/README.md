# FinAlly Backend

FastAPI backend for the FinAlly AI Trading Workstation.

## Structure

- `app/` - Application code
  - `main.py` - FastAPI app entry point (lifespan, SSE endpoint, static file serving)
  - `database.py` - SQLite data access layer (lazy init, all CRUD)
  - `models.py` - Pydantic request/response models
  - `routers/` - API route handlers
    - `portfolio.py` - Portfolio & trade endpoints
    - `watchlist.py` - Watchlist endpoints
    - `chat.py` - AI chat endpoint with LLM integration
    - `health.py` - Health check
  - `market/` - Market data subsystem
    - `models.py` - PriceUpdate dataclass
    - `cache.py` - Thread-safe price cache
    - `interface.py` - MarketDataSource abstract interface
    - `simulator.py` - GBM-based market simulator
    - `massive_client.py` - Massive/Polygon.io API client
    - `factory.py` - Data source factory
    - `stream.py` - SSE streaming router (used by main.py)
    - `seed_prices.py` - Default ticker prices and parameters

- `tests/` - Unit and integration tests
  - `market/` - Market data tests (73 tests)
  - `test_database.py` - Database layer tests
  - `test_portfolio.py` - Portfolio trade logic tests
  - `test_api.py` - Full API integration tests

## Running the Server

```bash
cd backend
uv sync --extra dev
uv run uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000/api/health` to verify it's running.

## Running Tests

```bash
cd backend

# Run all tests
uv run --extra dev pytest -v

# Run with coverage
uv run --extra dev pytest --cov=app --cov-report=html

# Run specific test modules
uv run --extra dev pytest tests/test_database.py -v
uv run --extra dev pytest tests/test_portfolio.py -v
uv run --extra dev pytest tests/test_api.py -v

# Run market data tests only
uv run --extra dev pytest tests/market/ -v
```

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | Yes (for chat) | — | OpenAI / OpenRouter API key |
| `OPENAI_BASE_URL` | No | OpenAI default | Override for OpenRouter |
| `DB_PATH` | No | `../../db/finally.db` | SQLite database file path |
| `MASSIVE_API_KEY` | No | — | Polygon.io API key (uses GBM simulator if unset) |
| `LLM_MOCK` | No | `false` | Set `true` for deterministic mock LLM responses |

## Development

```bash
cd backend

# Install dependencies
uv sync --extra dev

# Run linter
uv run --extra dev ruff check app/ tests/

# Format code
uv run --extra dev ruff format app/ tests/

# Demo: live market data terminal
uv run market_data_demo.py
```
