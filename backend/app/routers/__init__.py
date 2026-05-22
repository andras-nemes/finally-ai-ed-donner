"""Router package for FinAlly API."""

from .chat import router as chat_router
from .health import router as health_router
from .portfolio import router as portfolio_router
from .watchlist import router as watchlist_router

__all__ = ["portfolio_router", "watchlist_router", "chat_router", "health_router"]
