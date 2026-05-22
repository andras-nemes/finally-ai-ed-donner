"""Chat API endpoint.

POST /api/chat - Send a message to the AI assistant, receive structured response
                 with optional auto-executed trades and watchlist changes.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.database import (
    add_to_watchlist,
    get_chat_history,
    get_db,
    get_positions,
    get_user_profile,
    get_watchlist,
    record_chat_message,
    record_portfolio_snapshot,
    record_trade,
    remove_from_watchlist,
    update_cash_balance,
    upsert_position,
    get_position,
)
from app.models import (
    ChatRequest,
    ChatResponse,
    LLMStructuredResponse,
    TradeResult,
    WatchlistChangeResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# ---------------------------------------------------------------------------
# Mock response
# ---------------------------------------------------------------------------

_MOCK_RESPONSE = LLMStructuredResponse(
    message=(
        "Hello! I'm FinAlly, your AI trading assistant. "
        "I can analyze your portfolio, suggest trades, and execute them on your behalf. "
        "What would you like to do today?"
    ),
    trades=[],
    watchlist_changes=[],
)

# ---------------------------------------------------------------------------
# Portfolio context builder
# ---------------------------------------------------------------------------

def _build_context(conn, price_cache) -> tuple[dict, str]:
    """Build portfolio context dict and a human-readable table string."""
    profile = get_user_profile(conn)
    cash = profile["cash_balance"]
    positions = get_positions(conn)
    watchlist = get_watchlist(conn)

    total_market_value = 0.0
    total_pnl = 0.0
    position_lines = []

    for pos in positions:
        ticker = pos["ticker"]
        quantity = pos["quantity"]
        avg_cost = pos["avg_cost"]
        price_update = price_cache.get(ticker)
        current_price = price_update.price if price_update else None

        if current_price is not None:
            market_value = quantity * current_price
            pnl = market_value - quantity * avg_cost
            pnl_pct = (pnl / (quantity * avg_cost) * 100) if avg_cost != 0 else 0.0
            total_market_value += market_value
            total_pnl += pnl
            position_lines.append(
                f"  {ticker}: {quantity} shares @ avg ${avg_cost:.2f}, "
                f"current ${current_price:.2f}, P&L ${pnl:+.2f} ({pnl_pct:+.1f}%)"
            )
        else:
            position_lines.append(
                f"  {ticker}: {quantity} shares @ avg ${avg_cost:.2f}, price unavailable"
            )

    total_value = cash + total_market_value

    watchlist_lines = []
    for item in watchlist:
        ticker = item["ticker"]
        price_update = price_cache.get(ticker)
        price_str = f"${price_update.price:.2f}" if price_update else "N/A"
        watchlist_lines.append(f"  {ticker}: {price_str}")

    positions_text = "\n".join(position_lines) if position_lines else "  (no positions)"
    watchlist_text = "\n".join(watchlist_lines) if watchlist_lines else "  (empty)"

    context = {
        "cash_balance": cash,
        "total_value": total_value,
        "total_pnl": total_pnl,
        "positions_text": positions_text,
        "watchlist_text": watchlist_text,
    }
    return context, total_value


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------

def _build_system_prompt(context: dict) -> str:
    return (
        "You are FinAlly, an AI trading assistant for a simulated portfolio.\n"
        f"Current portfolio:\n"
        f"- Cash: ${context['cash_balance']:.2f}\n"
        f"- Total Value: ${context['total_value']:.2f}\n"
        f"- Total P&L: ${context['total_pnl']:+.2f}\n"
        f"Positions:\n{context['positions_text']}\n"
        f"Watchlist:\n{context['watchlist_text']}\n\n"
        "You analyze portfolios, suggest and execute trades, and manage the watchlist. "
        "Be concise and data-driven. Always respond with valid structured JSON."
    )


# ---------------------------------------------------------------------------
# Trade execution (shared with portfolio router logic)
# ---------------------------------------------------------------------------

def _execute_trade_in_conn(conn, price_cache, ticker: str, side: str, quantity: float) -> TradeResult:
    """Execute a single trade within an open connection. Returns a TradeResult."""
    ticker = ticker.upper()
    price_update = price_cache.get(ticker)

    if price_update is None:
        return TradeResult(
            ticker=ticker,
            side=side,
            quantity=quantity,
            executed=False,
            error=f"No price available for {ticker}",
        )

    price = price_update.price

    try:
        if side == "buy":
            cost = quantity * price
            profile = get_user_profile(conn)
            if profile["cash_balance"] < cost:
                return TradeResult(
                    ticker=ticker,
                    side=side,
                    quantity=quantity,
                    price=price,
                    executed=False,
                    error=(
                        f"Insufficient cash: need ${cost:.2f}, "
                        f"have ${profile['cash_balance']:.2f}"
                    ),
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

        else:  # sell
            existing = get_position(conn, ticker)
            owned = existing["quantity"] if existing else 0.0
            if owned < quantity:
                return TradeResult(
                    ticker=ticker,
                    side=side,
                    quantity=quantity,
                    price=price,
                    executed=False,
                    error=f"Insufficient shares: trying to sell {quantity}, own {owned}",
                )

            proceeds = quantity * price
            update_cash_balance(conn, proceeds)
            new_qty = owned - quantity
            avg_cost = existing["avg_cost"]
            upsert_position(conn, ticker, new_qty, avg_cost)

        record_trade(conn, ticker, side, quantity, price)

        return TradeResult(
            ticker=ticker,
            side=side,
            quantity=quantity,
            price=price,
            executed=True,
        )

    except Exception as exc:
        logger.exception("Trade execution error for %s", ticker)
        return TradeResult(
            ticker=ticker,
            side=side,
            quantity=quantity,
            price=price,
            executed=False,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Watchlist execution
# ---------------------------------------------------------------------------

def _execute_watchlist_change(conn, market_source_ref, ticker: str, action: str) -> WatchlistChangeResult:
    """Execute a watchlist add/remove. market_source_ref is a mutable list[market_source]."""
    ticker = ticker.upper()
    action = action.lower()

    try:
        if action == "add":
            add_to_watchlist(conn, ticker)
            return WatchlistChangeResult(ticker=ticker, action=action, executed=True)
        elif action == "remove":
            remove_from_watchlist(conn, ticker)
            return WatchlistChangeResult(ticker=ticker, action=action, executed=True)
        else:
            return WatchlistChangeResult(
                ticker=ticker,
                action=action,
                executed=False,
                error=f"Unknown action: {action!r}",
            )
    except ValueError as exc:
        return WatchlistChangeResult(
            ticker=ticker,
            action=action,
            executed=False,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("")
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    """Send a message to the AI assistant.

    The assistant may auto-execute trades and watchlist changes.
    If LLM_MOCK=true, returns a deterministic canned response.
    """
    price_cache = request.app.state.price_cache
    market_source = request.app.state.market_source
    db_path = request.app.state.db_path

    llm_mock = os.environ.get("LLM_MOCK", "false").lower() in ("true", "1", "yes")

    # Save user message
    with get_db(db_path) as conn:
        record_chat_message(conn, "user", body.message)

    if llm_mock:
        llm_response = _MOCK_RESPONSE
    else:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            error_msg = (
                "OPENAI_API_KEY is not set. Please configure it to use the AI assistant."
            )
            now = datetime.now(timezone.utc).isoformat()
            msg_id = str(uuid.uuid4())
            with get_db(db_path) as conn:
                result = record_chat_message(conn, "assistant", error_msg)
                msg_id = result["id"]
                now = result["created_at"]
            return ChatResponse(
                id=msg_id,
                message=error_msg,
                trades=[],
                watchlist_changes=[],
                created_at=now,
            )

        # Build context and call LLM
        with get_db(db_path) as conn:
            context, _ = _build_context(conn, price_cache)
            history = get_chat_history(conn, limit=20)

        system_prompt = _build_system_prompt(context)

        # Build message list for the API
        messages = [{"role": "system", "content": system_prompt}]
        for hist_msg in history:
            if hist_msg["role"] in ("user", "assistant"):
                messages.append({"role": hist_msg["role"], "content": hist_msg["content"]})
        messages.append({"role": "user", "content": body.message})

        try:
            from openai import OpenAI

            client = OpenAI()
            completion = client.beta.chat.completions.parse(
                model="gpt-4o",
                messages=messages,
                response_format=LLMStructuredResponse,
            )
            llm_response = completion.choices[0].message.parsed
            if llm_response is None:
                llm_response = LLMStructuredResponse(
                    message="I encountered an issue processing your request. Please try again.",
                    trades=[],
                    watchlist_changes=[],
                )
        except Exception as exc:
            logger.exception("LLM call failed")
            error_msg = f"AI assistant error: {exc}"
            with get_db(db_path) as conn:
                result = record_chat_message(conn, "assistant", error_msg)
            return ChatResponse(
                id=result["id"],
                message=error_msg,
                trades=[],
                watchlist_changes=[],
                created_at=result["created_at"],
            )

    # Auto-execute trades and watchlist changes
    trade_results: list[TradeResult] = []
    watchlist_results: list[WatchlistChangeResult] = []

    with get_db(db_path) as conn:
        # Execute trades
        for trade_instr in llm_response.trades:
            result = _execute_trade_in_conn(
                conn,
                price_cache,
                trade_instr.ticker,
                trade_instr.side,
                trade_instr.quantity,
            )
            trade_results.append(result)

        # Execute watchlist changes (pass market_source for async operations below)
        for wl_instr in llm_response.watchlist_changes:
            result = _execute_watchlist_change(conn, None, wl_instr.ticker, wl_instr.action)
            watchlist_results.append(result)

        # Take portfolio snapshot if any trades were executed
        if any(t.executed for t in trade_results):
            profile = get_user_profile(conn)
            cash = profile["cash_balance"]
            positions = get_positions(conn)
            total = cash
            for pos in positions:
                p = price_cache.get_price(pos["ticker"])
                if p:
                    total += pos["quantity"] * p
            record_portfolio_snapshot(conn, total)

        # Save assistant message with actions summary
        actions_summary = {
            "trades": [t.model_dump() for t in trade_results],
            "watchlist_changes": [w.model_dump() for w in watchlist_results],
        }
        saved = record_chat_message(conn, "assistant", llm_response.message, actions_summary)

    # Async: update market data source for watchlist changes
    for wl_instr, wl_result in zip(llm_response.watchlist_changes, watchlist_results):
        if wl_result.executed:
            ticker = wl_instr.ticker.upper()
            if wl_result.action == "add":
                await market_source.add_ticker(ticker)
            elif wl_result.action == "remove":
                await market_source.remove_ticker(ticker)

    return ChatResponse(
        id=saved["id"],
        message=llm_response.message,
        trades=trade_results,
        watchlist_changes=watchlist_results,
        created_at=saved["created_at"],
    )
