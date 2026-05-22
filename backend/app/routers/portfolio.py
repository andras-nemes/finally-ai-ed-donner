"""Portfolio API endpoints.

GET  /api/portfolio         - Current positions, cash, total value, P&L
POST /api/portfolio/trade   - Execute a market order (buy or sell)
GET  /api/portfolio/history - Portfolio value snapshots over time
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.database import (
    get_db,
    get_positions,
    get_portfolio_history,
    get_user_profile,
    record_portfolio_snapshot,
    record_trade,
    update_cash_balance,
    upsert_position,
    get_position,
)
from app.models import PortfolioResponse, PositionItem, TradeRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_portfolio_response(conn, price_cache) -> PortfolioResponse:
    """Construct a PortfolioResponse from DB and current prices."""
    profile = get_user_profile(conn)
    cash = profile["cash_balance"]
    positions_db = get_positions(conn)

    position_items: list[PositionItem] = []
    total_market_value = 0.0
    total_pnl = 0.0

    for pos in positions_db:
        ticker = pos["ticker"]
        quantity = pos["quantity"]
        avg_cost = pos["avg_cost"]

        price_update = price_cache.get(ticker)
        current_price = price_update.price if price_update else None

        if current_price is not None:
            market_value = quantity * current_price
            cost_basis = quantity * avg_cost
            unrealized_pnl = market_value - cost_basis
            unrealized_pnl_percent = (unrealized_pnl / cost_basis * 100) if cost_basis != 0 else 0.0
            total_market_value += market_value
            total_pnl += unrealized_pnl
        else:
            market_value = None
            unrealized_pnl = None
            unrealized_pnl_percent = None

        position_items.append(
            PositionItem(
                ticker=ticker,
                quantity=quantity,
                avg_cost=avg_cost,
                current_price=current_price,
                market_value=market_value,
                unrealized_pnl=unrealized_pnl,
                unrealized_pnl_percent=unrealized_pnl_percent,
            )
        )

    total_value = cash + total_market_value

    return PortfolioResponse(
        cash_balance=cash,
        positions=position_items,
        total_value=total_value,
        total_pnl=total_pnl,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def get_portfolio(request: Request) -> PortfolioResponse:
    """Return current portfolio: positions with live P&L, cash balance, totals."""
    price_cache = request.app.state.price_cache
    db_path = request.app.state.db_path

    with get_db(db_path) as conn:
        return _build_portfolio_response(conn, price_cache)


@router.post("/trade")
async def execute_trade(trade_req: TradeRequest, request: Request) -> dict:
    """Execute a market order.

    Buy: requires sufficient cash (quantity * current_price).
    Sell: requires sufficient shares owned.
    Returns the trade record and updated portfolio summary.
    """
    price_cache = request.app.state.price_cache
    db_path = request.app.state.db_path

    ticker = trade_req.ticker
    quantity = trade_req.quantity
    side = trade_req.side

    # Look up current price
    price_update = price_cache.get(ticker)
    if price_update is None:
        raise HTTPException(
            status_code=422,
            detail=f"No price available for {ticker}. "
                   "Add it to your watchlist first.",
        )

    price = price_update.price

    with get_db(db_path) as conn:
        if side == "buy":
            cost = quantity * price
            profile = get_user_profile(conn)
            if profile["cash_balance"] < cost:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Insufficient cash: need ${cost:.2f}, "
                        f"have ${profile['cash_balance']:.2f}"
                    ),
                )

            # Deduct cash
            update_cash_balance(conn, -cost)

            # Update position with new weighted avg cost
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

        else:  # sell
            existing = get_position(conn, ticker)
            owned = existing["quantity"] if existing else 0.0
            if owned < quantity:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Insufficient shares: trying to sell {quantity}, "
                        f"own {owned}"
                    ),
                )

            proceeds = quantity * price
            update_cash_balance(conn, proceeds)

            new_qty = owned - quantity
            avg_cost = existing["avg_cost"]
            upsert_position(conn, ticker, new_qty, avg_cost)

        # Record trade
        trade_record = record_trade(conn, ticker, side, quantity, price)

        # Snapshot portfolio value immediately
        portfolio = _build_portfolio_response(conn, price_cache)
        record_portfolio_snapshot(conn, portfolio.total_value)

    logger.info("Trade executed: %s %s x%s @ $%.2f", side.upper(), ticker, quantity, price)

    return {
        "trade": trade_record,
        "portfolio": portfolio.model_dump(),
    }


@router.get("/history")
async def get_portfolio_history_endpoint(request: Request) -> list[dict]:
    """Return portfolio value snapshots (up to 200, oldest first)."""
    db_path = request.app.state.db_path

    with get_db(db_path) as conn:
        return get_portfolio_history(conn, limit=200)
