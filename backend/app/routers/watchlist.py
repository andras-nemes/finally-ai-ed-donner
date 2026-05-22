"""Watchlist API endpoints.

GET    /api/watchlist         - Current watchlist with live prices
POST   /api/watchlist         - Add a ticker
DELETE /api/watchlist/{ticker} - Remove a ticker
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.database import (
    add_to_watchlist,
    get_watchlist,
    get_db,
    remove_from_watchlist,
)
from app.models import AddWatchlistRequest, WatchlistItem

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


def _enrich_with_price(item: dict, price_cache) -> WatchlistItem:
    """Combine a watchlist DB row with live price data from the cache."""
    ticker = item["ticker"]
    price_update = price_cache.get(ticker)

    if price_update is not None:
        return WatchlistItem(
            ticker=ticker,
            price=price_update.price,
            previous_price=price_update.previous_price,
            change=price_update.change,
            change_percent=price_update.change_percent,
            direction=price_update.direction,
            added_at=item["added_at"],
        )

    return WatchlistItem(
        ticker=ticker,
        price=None,
        previous_price=None,
        change=None,
        change_percent=None,
        direction=None,
        added_at=item["added_at"],
    )


@router.get("")
async def get_watchlist_endpoint(request: Request) -> list[WatchlistItem]:
    """Return the current watchlist enriched with live prices."""
    price_cache = request.app.state.price_cache
    db_path = request.app.state.db_path

    with get_db(db_path) as conn:
        items = get_watchlist(conn)

    return [_enrich_with_price(item, price_cache) for item in items]


@router.post("")
async def add_ticker(body: AddWatchlistRequest, request: Request) -> WatchlistItem:
    """Add a ticker to the watchlist and start streaming its price."""
    ticker = body.ticker
    price_cache = request.app.state.price_cache
    market_source = request.app.state.market_source
    db_path = request.app.state.db_path

    try:
        with get_db(db_path) as conn:
            new_row = add_to_watchlist(conn, ticker)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Start the market data source tracking this ticker
    await market_source.add_ticker(ticker)

    logger.info("Added %s to watchlist", ticker)

    return _enrich_with_price(new_row, price_cache)


@router.delete("/{ticker}")
async def remove_ticker(ticker: str, request: Request) -> dict:
    """Remove a ticker from the watchlist and stop streaming its price."""
    ticker = ticker.upper()
    market_source = request.app.state.market_source
    db_path = request.app.state.db_path

    try:
        with get_db(db_path) as conn:
            remove_from_watchlist(conn, ticker)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await market_source.remove_ticker(ticker)

    logger.info("Removed %s from watchlist", ticker)

    return {"message": f"{ticker} removed from watchlist"}
