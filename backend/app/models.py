"""Pydantic v2 models for FinAlly API requests and responses."""

from __future__ import annotations

from pydantic import BaseModel, field_validator


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TradeRequest(BaseModel):
    ticker: str
    quantity: float
    side: str

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("quantity")
    @classmethod
    def quantity_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("quantity must be positive")
        return v

    @field_validator("side")
    @classmethod
    def side_valid(cls, v: str) -> str:
        v = v.lower()
        if v not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        return v


class AddWatchlistRequest(BaseModel):
    ticker: str

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, v: str) -> str:
        return v.strip().upper()


class ChatRequest(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class PositionItem(BaseModel):
    ticker: str
    quantity: float
    avg_cost: float
    current_price: float | None
    market_value: float | None
    unrealized_pnl: float | None
    unrealized_pnl_percent: float | None


class PortfolioResponse(BaseModel):
    cash_balance: float
    positions: list[PositionItem]
    total_value: float
    total_pnl: float  # sum of unrealized P&L across all positions


class WatchlistItem(BaseModel):
    ticker: str
    price: float | None
    previous_price: float | None
    change: float | None
    change_percent: float | None
    direction: str | None
    added_at: str


class TradeResult(BaseModel):
    ticker: str
    side: str
    quantity: float
    price: float | None = None
    executed: bool
    error: str | None = None


class WatchlistChangeResult(BaseModel):
    ticker: str
    action: str
    executed: bool
    error: str | None = None


class ChatResponse(BaseModel):
    id: str
    message: str
    trades: list[TradeResult]
    watchlist_changes: list[WatchlistChangeResult]
    created_at: str


# ---------------------------------------------------------------------------
# Internal models for LLM structured output
# ---------------------------------------------------------------------------

class LLMTradeInstruction(BaseModel):
    ticker: str
    side: str
    quantity: float


class LLMWatchlistInstruction(BaseModel):
    ticker: str
    action: str  # 'add' or 'remove'


class LLMStructuredResponse(BaseModel):
    message: str
    trades: list[LLMTradeInstruction] = []
    watchlist_changes: list[LLMWatchlistInstruction] = []
