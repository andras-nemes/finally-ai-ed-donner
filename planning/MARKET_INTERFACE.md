# Market Data Interface Design

Unified Python interface for market data in FinAlly. Two implementations (GBM simulator and Massive API) sit behind one abstract interface. All downstream code — SSE streaming, portfolio valuation, trade execution — is source-agnostic.

This document uses imaginary tickers (FLUX, ZEPH, NOVA, CRUX, ORBT) as examples throughout.

## Design Goals

- Downstream code never imports `SimulatorDataSource` or `MassiveDataSource` directly
- Swapping data sources is a one-line change in the factory function
- The `PriceCache` is the single source of truth for current prices
- The interface is async to support long-running background tasks cleanly

## Core Data Model

`PriceUpdate` is the only data structure that leaves the market data layer. Everything downstream works with these objects.

```python
from dataclasses import dataclass, field
import time

@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

## Abstract Interface

```python
from abc import ABC, abstractmethod

class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices
    — it reads from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["FLUX", "ZEPH", "NOVA"])
        await source.add_ticker("CRUX")
        await source.remove_ticker("ZEPH")
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Call exactly once. Calling start() twice is undefined behavior.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. Also removes it from the cache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

## Price Cache

The `PriceCache` is a shared in-memory store. Data sources write to it; SSE streaming, portfolio valuation, and trade execution read from it. No component reads prices directly from the data source.

```python
import time
from threading import Lock

class PriceCache:
    """Thread-safe cache of the latest price per ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one writer at a time).
    Readers: SSE endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Bumped on every update; used for SSE change detection

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Returns the PriceUpdate that was stored."""
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices (shallow copy)."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Monotonic counter. The SSE generator polls this to detect changes."""
        return self._version
```

## Factory Function

Selects the data source at startup based on environment variables.

```python
import os

def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Return the appropriate data source based on environment.

    - MASSIVE_API_KEY set and non-empty -> MassiveDataSource (real market data)
    - Otherwise -> SimulatorDataSource (GBM simulation, no external dependencies)

    Returns an unstarted source. Caller must call await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource
        return SimulatorDataSource(price_cache=price_cache)
```

## Simulator Implementation (Sketch)

Full detail in `MARKET_SIMULATOR.md`. The `SimulatorDataSource` wraps `GBMSimulator` in an asyncio loop.

```python
import asyncio

class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5):
        self._cache = price_cache
        self._interval = update_interval
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers)
        # Seed the cache immediately so the SSE endpoint has data on first connect
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                pass  # Log and continue; don't crash the loop
            await asyncio.sleep(self._interval)
```

## Massive Implementation (Sketch)

Full detail in `MASSIVE_API.md`. The `MassiveDataSource` polls the REST API on a timer.

```python
import asyncio
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0):
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._client: RESTClient | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()  # Seed cache immediately
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._client = None

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            snapshots = await asyncio.to_thread(
                self._client.get_snapshot_all,
                market_type=SnapshotMarketType.STOCKS,
                tickers=self._tickers,
            )
            for snap in snapshots:
                self._cache.update(
                    ticker=snap.ticker,
                    price=snap.last_trade.price,
                    timestamp=snap.last_trade.timestamp / 1000,  # ms -> seconds
                )
        except Exception as e:
            pass  # Log error; retry on next poll interval
```

## Integration with SSE

The SSE endpoint reads from the `PriceCache` using the `version` counter to avoid redundant pushes when prices haven't changed.

```python
import asyncio
import json
from fastapi import Request
from fastapi.responses import StreamingResponse

async def stream_prices(request: Request, price_cache: PriceCache) -> StreamingResponse:
    return StreamingResponse(
        _generate_events(price_cache, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )

async def _generate_events(price_cache: PriceCache, request: Request):
    yield "retry: 1000\n\n"
    last_version = -1

    while True:
        if await request.is_disconnected():
            break

        current_version = price_cache.version
        if current_version != last_version:
            last_version = current_version
            prices = price_cache.get_all()
            if prices:
                data = {ticker: update.to_dict() for ticker, update in prices.items()}
                yield f"data: {json.dumps(data)}\n\n"

        await asyncio.sleep(0.5)
```

Example SSE event (imaginary FLUX ticker):
```
data: {"FLUX": {"ticker": "FLUX", "price": 146.23, "previous_price": 145.60, "timestamp": 1700000000.0, "change": 0.63, "change_percent": 0.4326, "direction": "up"}}
```

## File Structure

```
backend/
  app/
    market/
      __init__.py          # Public API: PriceUpdate, PriceCache, MarketDataSource,
                           #             create_market_data_source, create_stream_router
      models.py            # PriceUpdate frozen dataclass
      interface.py         # MarketDataSource ABC
      cache.py             # PriceCache implementation
      factory.py           # create_market_data_source()
      simulator.py         # GBMSimulator + SimulatorDataSource
      massive_client.py    # MassiveDataSource
      seed_prices.py       # SEED_PRICES, TICKER_PARAMS, correlation constants
      stream.py            # create_stream_router() — SSE FastAPI router
```

## Application Lifecycle

```python
# App startup (e.g., FastAPI lifespan)
cache = PriceCache()
source = create_market_data_source(cache)
await source.start(["FLUX", "ZEPH", "NOVA", "CRUX", "ORBT"])

# Reading prices (SSE, trade execution, portfolio valuation)
update = cache.get("FLUX")         # PriceUpdate | None
price = cache.get_price("NOVA")    # float | None
all_prices = cache.get_all()       # dict[str, PriceUpdate]

# Watchlist changes
await source.add_ticker("ECHO")
await source.remove_ticker("ZEPH")

# App shutdown
await source.stop()
```

The calling code (FastAPI app, route handlers) never needs to know whether prices come from the simulator or the Massive API.
