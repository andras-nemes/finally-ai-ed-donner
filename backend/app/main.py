"""FinAlly FastAPI application entry point.

Serves:
- API routes under /api/*
- SSE streaming under /api/stream/prices
- Static Next.js frontend from /
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

# Load .env from project root (two levels up from app/)
try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).parent.parent.parent / ".env"
    load_dotenv(_env_path)
except ImportError:
    pass

from app.database import (
    get_db,
    get_positions,
    get_user_profile,
    get_watchlist_tickers,
    init_db,
    record_portfolio_snapshot,
)
from app.market import PriceCache, create_market_data_source
from app.routers import chat_router, health_router, portfolio_router, watchlist_router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Background task: portfolio snapshots every 30 seconds
# ---------------------------------------------------------------------------


async def _portfolio_snapshot_loop(price_cache: PriceCache, db_path: str) -> None:
    """Record portfolio value snapshot every 30 seconds."""
    while True:
        await asyncio.sleep(30)
        try:
            path = db_path or None
            with get_db(path) as conn:
                profile = get_user_profile(conn)
                cash = profile["cash_balance"]
                positions = get_positions(conn)
                total = cash
                for pos in positions:
                    price = price_cache.get_price(pos["ticker"])
                    if price:
                        total += pos["quantity"] * price
                record_portfolio_snapshot(conn, total)
        except Exception:
            logger.exception("Portfolio snapshot failed")


# ---------------------------------------------------------------------------
# Lifespan: startup / shutdown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Determine DB path (may be None → uses default)
    db_path_env = os.environ.get("DB_PATH", "") or None

    # Initialize (create schema + seed if needed)
    init_db(db_path_env)

    # Create shared state
    price_cache = PriceCache()
    market_source = create_market_data_source(price_cache)

    # Load initial tickers from the watchlist
    with get_db(db_path_env) as conn:
        tickers = get_watchlist_tickers(conn)

    # Start market data simulator / Massive
    await market_source.start(tickers)

    # Expose on app.state for routers
    app.state.price_cache = price_cache
    app.state.market_source = market_source
    app.state.db_path = db_path_env

    # Background snapshot task
    snapshot_task = asyncio.create_task(
        _portfolio_snapshot_loop(price_cache, db_path_env or "")
    )

    logger.info("FinAlly backend started — tracking %d tickers", len(tickers))

    yield

    # Cleanup on shutdown
    snapshot_task.cancel()
    try:
        await snapshot_task
    except asyncio.CancelledError:
        pass

    await market_source.stop()
    logger.info("FinAlly backend stopped")


# ---------------------------------------------------------------------------
# SSE streaming endpoint (inline — reads from app.state.price_cache)
# ---------------------------------------------------------------------------


async def _sse_event_generator(price_cache: PriceCache, request: Request):
    """Async generator that yields SSE-formatted events for all tracked tickers."""
    # Tell clients to retry after 1 second on disconnect
    yield "retry: 1000\n\n"

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    payload = json.dumps(data)
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(title="FinAlly API", version="0.1.0", lifespan=lifespan)

    # Include API routers
    app.include_router(health_router)
    app.include_router(portfolio_router)
    app.include_router(watchlist_router)
    app.include_router(chat_router)

    # Inline SSE route (reads price_cache from app.state at request time)
    @app.get("/api/stream/prices", tags=["streaming"])
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint — streams all ticker prices every ~500ms."""
        price_cache = request.app.state.price_cache
        return StreamingResponse(
            _sse_event_generator(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Static file serving — Next.js export (mounted last so API routes take priority)
    static_dir = Path(__file__).parent.parent / "static"
    if static_dir.exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
        logger.info("Serving static files from %s", static_dir)

    return app


app = create_app()
