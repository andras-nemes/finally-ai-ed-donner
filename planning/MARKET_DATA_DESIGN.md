# Market Data Backend — Design Document

Implementation-ready design for the FinAlly market data subsystem. Covers the
unified interface, in-memory price cache, GBM ticker simulator, SSE streaming
endpoint, and FastAPI lifecycle integration.

Everything lives under `backend/app/market/`.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model)
4. [Price Cache — `cache.py`](#4-price-cache)
5. [Abstract Interface — `interface.py`](#5-abstract-interface)
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator)
8. [Factory — `factory.py`](#8-factory)
9. [SSE Streaming Endpoint — `stream.py`](#9-sse-streaming-endpoint)
10. [FastAPI Lifecycle Integration](#10-fastapi-lifecycle-integration)
11. [Watchlist Coordination](#11-watchlist-coordination)
12. [Testing Strategy](#12-testing-strategy)
13. [Error Handling & Edge Cases](#13-error-handling--edge-cases)
14. [Configuration Summary](#14-configuration-summary)

---

## 1. Architecture Overview

The market data subsystem uses a **strategy pattern**. A single abstract
interface (`MarketDataSource`) has one primary implementation: the GBM
simulator. All downstream code — SSE streaming, portfolio valuation, trade
execution — reads exclusively from a shared `PriceCache` and never touches the
data source directly.

```
GBMSimulator (math engine)
      │ step() every 500ms
      ▼
SimulatorDataSource (async wrapper)
      │ cache.update(ticker, price)
      ▼
PriceCache (thread-safe, in-memory)
      │
      ├──▶ GET /api/stream/prices  (SSE → frontend)
      ├──▶ Portfolio valuation
      └──▶ Trade execution
```

### Data flow, step by step

1. On app startup, `SimulatorDataSource.start(tickers)` is called with the
   default watchlist from the database.
2. `GBMSimulator` initialises seed prices for all tickers and computes a
   Cholesky decomposition of the sector-based correlation matrix.
3. A background asyncio task (`_run_loop`) calls `GBMSimulator.step()` every
   500 ms, which advances all prices by one GBM time step.
4. Each new price is written to `PriceCache` via `cache.update()`, which bumps
   a monotonic version counter.
5. The SSE generator polls `price_cache.version` every 500 ms. When the
   version changes it serialises all prices and yields them to the connected
   browser.
6. The browser `EventSource` receives the JSON blob and updates the UI.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py       # Public re-exports
      models.py         # PriceUpdate frozen dataclass
      cache.py          # PriceCache — thread-safe in-memory store
      interface.py      # MarketDataSource ABC
      seed_prices.py    # SEED_PRICES, TICKER_PARAMS, correlation constants
      simulator.py      # GBMSimulator + SimulatorDataSource
      factory.py        # create_market_data_source() — selects implementation
      stream.py         # FastAPI SSE router factory
```

`__init__.py` re-exports the public API so the rest of the backend imports
from `app.market` without knowing which submodule anything lives in:

```python
# backend/app/market/__init__.py
from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
]
```

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the **only** data structure that leaves the market data layer.
Every consumer — SSE streaming, portfolio valuation, trade execution — works
with this type.

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


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

### Design decisions

| Decision | Rationale |
|---|---|
| `frozen=True` | Value objects never mutate; safe to share across async tasks without copying |
| `slots=True` | Memory optimisation — hundreds are created per second |
| Computed properties | `change` and `direction` derive from the stored fields and can never be stale |
| `to_dict()` | Single serialisation point used by both the SSE endpoint and REST API responses |

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The price cache is the central data hub. The data source writes to it;
everything else reads from it. It must be thread-safe because the simulator's
`step()` is called from the asyncio event loop while SSE reads also happen on
the event loop — but `asyncio.to_thread()` calls (if ever used) operate in
real OS threads.

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writer: SimulatorDataSource (one writer at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Returns the PriceUpdate stored.

        On the first update for a ticker, previous_price == price (direction='flat').
        """
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
        """Monotonic counter — bumped on every update. Used by SSE for change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why a version counter?

The SSE loop polls the cache every 500 ms. Without a version counter, it would
serialise and push all prices on every poll even if nothing changed. The version
lets the loop skip sends cheaply:

```python
last_version = -1
while True:
    current_version = price_cache.version
    if current_version != last_version:
        last_version = current_version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Why `threading.Lock` and not `asyncio.Lock`?

`asyncio.Lock` only protects against concurrent coroutines on the same event
loop. `threading.Lock` is a real OS mutex that also protects against threads
spawned by `asyncio.to_thread()`. Using `threading.Lock` is always safe here
and keeps the API simple — callers do not need to `await` lock acquisition.

---

## 5. Abstract Interface

**File: `backend/app/market/interface.py`**

The ABC establishes the lifecycle contract. Every data source — today the
simulator, tomorrow anything else — must honour this interface. Downstream code
(SSE, portfolio routes, watchlist routes) never imports `SimulatorDataSource`
directly; it only imports `MarketDataSource`.

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source for prices — it reads
    from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", "MSFT", ...])
        await source.add_ticker("PYPL")
        await source.remove_ticker("GOOGL")
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task. Call exactly once. Calling start() twice is
        undefined behaviour.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times. After stop(), no further writes to cache.
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

### Why the source writes to the cache (push model)

The alternative — polling the source from the SSE endpoint — would couple the
SSE cadence to the data source update rate. The push model keeps these
independent: the simulator always ticks at 500 ms regardless of how many SSE
clients are connected or how often they poll.

---

## 6. Seed Prices & Ticker Parameters

**File: `backend/app/market/seed_prices.py`**

Pure constants, no logic. The simulator reads these at initialisation time to
set starting prices and per-ticker GBM parameters.

```python
"""Seed prices and per-ticker parameters for the market simulator."""

# Starting prices for the default watchlist tickers
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters.
# sigma: annualised volatility  (higher → more price movement per tick)
# mu:    annualised drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # High vol + strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # Low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # Low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Fallback for tickers not in the list (dynamically added at runtime)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Sector groups for the Cholesky correlation model
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Pairwise correlation coefficients
INTRA_TECH_CORR    = 0.6   # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR   = 0.3   # Cross-sector and unknown tickers
TSLA_CORR          = 0.3   # TSLA is in the tech group but does its own thing
```

### Parameter rationale

| Ticker | sigma | Character |
|--------|-------|-----------|
| V, JPM | 0.17–0.18 | Large-cap financials — stable, slow-moving |
| MSFT, AAPL | 0.20–0.22 | Mature tech — moderate |
| GOOGL, AMZN | 0.25–0.28 | Growth tech — slightly more variable |
| META, NFLX | 0.30–0.35 | Platform risk — more volatile |
| NVDA | 0.40 | AI darling — high vol but strong positive drift |
| TSLA | 0.50 | Speculative — very high vol, low correlation with peers |

Tickers added at runtime (not in the table) get `sigma=0.25, mu=0.05` and a
random seed price between $50 and $300.

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

This file contains two classes:

- **`GBMSimulator`** — pure math engine; stateful, holds current prices
- **`SimulatorDataSource`** — the `MarketDataSource` implementation; wraps
  `GBMSimulator` in an asyncio background task and writes to `PriceCache`

### 7.1 The GBM Math

Geometric Brownian Motion is the standard model for equity prices. The key
property is that prices are always positive because the model is multiplicative
(uses `exp()`). At each time step:

```
S(t + dt) = S(t) * exp( (mu - sigma²/2) * dt  +  sigma * sqrt(dt) * Z )
              ▲               ▲                           ▲
          current price    Itô drift correction      stochastic shock
```

| Symbol | Meaning |
|--------|---------|
| `S(t)` | Current price |
| `mu` | Annualised drift (e.g. `0.05` = 5% expected annual return) |
| `sigma` | Annualised volatility (e.g. `0.25` = 25% annual vol) |
| `dt` | Time step as a fraction of a trading year |
| `Z` | Standard normal random variable N(0, 1) |

The `- sigma²/2` (Itô correction) ensures the **expected** price growth is
`mu`, not `mu + sigma²/2`. Without it the model drifts upward faster than
intended.

#### Choosing `dt`

```
trading seconds per year = 252 days × 6.5 hours × 3600 s/hr = 5,896,800

dt = 0.5 s / 5,896,800 s/yr ≈ 8.48 × 10⁻⁸
```

Per-tick standard deviation for `sigma = 0.25`:

```
sigma × sqrt(dt) = 0.25 × sqrt(8.48e-8) ≈ 0.0073%
```

After a full simulated trading day (~46,800 ticks at 500 ms):

```
daily vol ≈ sigma / sqrt(252) = 0.25 / 15.87 ≈ 1.57%
```

That matches a typical mid-cap tech stock — realistic.

### 7.2 Correlated Moves via Cholesky Decomposition

Real stocks in the same sector tend to move together (e.g., when AAPL drops,
MSFT usually drops too). This is modelled with **Cholesky decomposition** of
the sector-based correlation matrix.

Given a symmetric positive-definite correlation matrix `C`, its Cholesky
factor `L` satisfies `L @ L.T = C`. Multiplying a vector of independent
normals by `L` produces a vector with the desired cross-correlations:

```python
z_independent = np.random.standard_normal(n)   # n iid N(0,1) draws
z_correlated  = L @ z_independent              # now correlated per C
```

The correlation matrix is rebuilt in O(n²) whenever tickers are added or
removed — with n < 50 this runs in microseconds.

### 7.3 Shock Events

Every tick, each ticker has a 0.1% chance of a sudden ±2–5% shock. This adds
visual drama:

```python
EVENT_PROBABILITY = 0.001  # per ticker per tick

if random.random() < EVENT_PROBABILITY:
    shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
    price *= (1 + shock)
```

With 10 tickers at 2 ticks/second, expect roughly one shock event somewhere
every 50 seconds.

### 7.4 GBMSimulator Implementation

```python
from __future__ import annotations

import asyncio
import logging
import math
import random

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Correlated GBM price simulator for multiple tickers.

    Math: S(t+dt) = S(t) * exp((mu - sigma²/2)*dt + sigma*sqrt(dt)*Z)
    """

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance all tickers one time step. Returns {ticker: new_price}.

        Hot path — called every 500 ms.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu    = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]

            drift     = (mu - 0.5 * sigma ** 2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1 + shock)
                logger.debug("Shock on %s: %+.1f%%", ticker, shock * 100)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add a ticker without rebuilding Cholesky (used during batch init)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        tech    = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

### 7.5 SimulatorDataSource — Async Wrapper

`SimulatorDataSource` satisfies the `MarketDataSource` ABC. It drives
`GBMSimulator` from a background asyncio task and writes results to
`PriceCache`.

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by GBMSimulator.

    Runs a background asyncio task that calls GBMSimulator.step() every
    `update_interval` seconds and writes results to PriceCache.
    """

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache immediately so SSE has data on the very first connect
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
            logger.info("Simulator: added %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        """Core loop: step the sim, write to cache, sleep, repeat."""
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

#### Key behaviours

| Behaviour | Detail |
|---|---|
| **Immediate seeding** | `start()` populates the cache with seed prices *before* launching the loop. The SSE endpoint has data on its first poll — no blank-screen delay. |
| **Graceful cancellation** | `stop()` cancels the task and awaits it, catching `CancelledError`. Clean shutdown during FastAPI lifespan teardown. |
| **Exception resilience** | The loop catches all exceptions per-step so a single bad tick cannot kill the data feed. |
| **Prices always positive** | GBM uses `exp()` — mathematically impossible to go negative. No runtime guards needed. |

---

## 8. Factory

**File: `backend/app/market/factory.py`**

Selects the data source at startup. For now, with no `MASSIVE_API_KEY`, this
always returns `SimulatorDataSource`.

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Return the appropriate market data source based on environment.

    - MASSIVE_API_KEY set and non-empty  →  MassiveDataSource (real data)
    - Otherwise                          →  SimulatorDataSource (GBM sim)

    Returns an **unstarted** source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

### Usage at startup

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                    "NVDA", "META", "JPM", "V", "NFLX"])
```

---

## 9. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

Server-Sent Events (SSE) is a one-way HTTP push protocol. The browser opens a
single long-lived connection to `/api/stream/prices` using the native
`EventSource` API. The server pushes newline-delimited text events.

```python
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Return the SSE router, injecting the shared PriceCache via closure."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",   # Disable nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted price events."""
    yield "retry: 1000\n\n"   # Tell the browser to reconnect after 1 s if dropped

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
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### Wire format

Each SSE event the browser receives:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,
       "timestamp":1748912000.5,"change":0.08,"change_percent":0.042,
       "direction":"up"},"GOOGL":{"ticker":"GOOGL","price":175.12,...}}

```

(Two newlines terminate each event — that is the SSE spec.)

### Browser-side connection

```javascript
const eventSource = new EventSource('/api/stream/prices');

eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices: { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }
    updateWatchlist(prices);
};

eventSource.onerror = () => {
    // EventSource reconnects automatically after `retry` ms.
    // Show the yellow "reconnecting" status indicator here.
    setConnectionStatus('reconnecting');
};
```

### Design notes

- **Version-based change detection** prevents pushing identical payloads when
  no prices have changed.
- **`retry: 1000`** instructs the browser to wait 1 second before reconnecting
  after a dropped connection. `EventSource` handles this automatically.
- **`X-Accel-Buffering: no`** prevents nginx from batching SSE frames if the
  app is deployed behind a reverse proxy.
- **Poll-and-push over event-driven**: polling on a fixed interval produces
  evenly-spaced events, which is important for smooth sparkline chart updates.

---

## 10. FastAPI Lifecycle Integration

Market data starts and stops with the FastAPI application using the
`lifespan` context manager.

**`backend/app/main.py`**:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── STARTUP ─────────────────────────────────────────────────────────────

    # 1. Shared price cache (single instance, referenced everywhere)
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 2. Create and start the market data source
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 3. Load the initial watchlist from the database
    initial_tickers = await load_watchlist_from_db()    # queries SQLite watchlist table
    await source.start(initial_tickers)

    # 4. Register the SSE router (after cache is populated)
    app.include_router(create_stream_router(price_cache))

    yield   # ── APP IS RUNNING ──────────────────────────────────────────────

    # ── SHUTDOWN ─────────────────────────────────────────────────────────────
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)

# Serve the Next.js static export at the root
app.mount("/", StaticFiles(directory="static", html=True), name="frontend")


# ── FastAPI dependency helpers ────────────────────────────────────────────────

def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Accessing the cache from route handlers

```python
from fastapi import APIRouter, Depends, HTTPException
from app.market import PriceCache, MarketDataSource
from app.main import get_price_cache, get_market_source

router = APIRouter(prefix="/api")


@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(400, f"No price available for {trade.ticker!r}. Try again shortly.")
    # ... execute trade at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
    price_cache: PriceCache = Depends(get_price_cache),
):
    await db.insert_watchlist(payload.ticker)
    await source.add_ticker(payload.ticker)
    price = price_cache.get_price(payload.ticker)
    return {"ticker": payload.ticker, "price": price}


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist(ticker)
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)
    return {"status": "ok"}
```

---

## 11. Watchlist Coordination

When the watchlist changes, the data source must be notified immediately so
it tracks the correct set of tickers.

### Adding a ticker

```
POST /api/watchlist  { "ticker": "PYPL" }
  1. Insert into watchlist table (SQLite)
  2. await source.add_ticker("PYPL")
       → GBMSimulator.add_ticker("PYPL")
           → _add_ticker_internal: seed price $50–300, default params
           → _rebuild_cholesky (microseconds)
       → cache.update("PYPL", seed_price)  ← immediately visible in SSE
  3. Return { ticker: "PYPL", price: <seed_price> }
```

### Removing a ticker

```
DELETE /api/watchlist/PYPL
  1. Delete from watchlist table (SQLite)
  2. Check for open position in positions table
     If quantity > 0: keep tracking (needed for portfolio valuation)
     If quantity == 0: await source.remove_ticker("PYPL")
                           → GBMSimulator.remove_ticker
                           → cache.remove("PYPL")
  3. Return { status: "ok" }
```

### Why keep tracking tickers with open positions?

If the user removes a ticker from the watchlist but still holds shares, the
portfolio valuation (`GET /api/portfolio`) needs a current price to compute
unrealized P&L. Removing from `PriceCache` would break that calculation.
The data source continues to simulate the ticker; it just no longer appears
in the watchlist GET response.

---

## 12. Testing Strategy

Tests live in `backend/tests/market/`. Run with:

```bash
cd backend
uv run --extra dev pytest -v tests/market/
uv run --extra dev pytest --cov=app tests/market/
```

### 12.1 `test_models.py` — PriceUpdate

```python
from app.market.models import PriceUpdate


def test_direction_up():
    u = PriceUpdate(ticker="AAPL", price=191.00, previous_price=190.00)
    assert u.direction == "up"
    assert u.change == 1.00
    assert u.change_percent == pytest.approx(0.5263, rel=1e-3)


def test_direction_down():
    u = PriceUpdate(ticker="AAPL", price=189.00, previous_price=190.00)
    assert u.direction == "down"


def test_direction_flat():
    u = PriceUpdate(ticker="AAPL", price=190.00, previous_price=190.00)
    assert u.direction == "flat"
    assert u.change == 0.0


def test_to_dict_keys():
    u = PriceUpdate(ticker="AAPL", price=190.00, previous_price=190.00)
    d = u.to_dict()
    assert set(d.keys()) == {
        "ticker", "price", "previous_price", "timestamp",
        "change", "change_percent", "direction",
    }


def test_frozen():
    u = PriceUpdate(ticker="AAPL", price=190.00, previous_price=190.00)
    with pytest.raises(Exception):
        u.price = 200.00  # type: ignore[misc]
```

### 12.2 `test_cache.py` — PriceCache

```python
from app.market.cache import PriceCache


def test_first_update_is_flat():
    cache = PriceCache()
    u = cache.update("AAPL", 190.50)
    assert u.direction == "flat"
    assert u.previous_price == 190.50


def test_version_increments():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    assert cache.version == v0 + 1
    cache.update("AAPL", 191.00)
    assert cache.version == v0 + 2


def test_direction_tracked():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    u = cache.update("AAPL", 191.00)
    assert u.direction == "up"
    assert u.previous_price == 190.00


def test_remove():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None


def test_get_all_snapshot():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.update("GOOGL", 175.00)
    snapshot = cache.get_all()
    assert set(snapshot.keys()) == {"AAPL", "GOOGL"}


def test_get_price_convenience():
    cache = PriceCache()
    cache.update("AAPL", 190.50)
    assert cache.get_price("AAPL") == 190.50
    assert cache.get_price("ZZZZ") is None
```

### 12.3 `test_simulator.py` — GBMSimulator

```python
import pytest
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


class TestGBMSimulator:

    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}

    def test_prices_always_positive(self):
        """GBM prices cannot go negative (exp() is always positive)."""
        sim = GBMSimulator(tickers=["TSLA"], event_probability=0.0)
        for _ in range(10_000):
            prices = sim.step()
            assert prices["TSLA"] > 0

    def test_initial_prices_match_seeds(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_add_ticker_appears_in_step(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("TSLA")
        result = sim.step()
        assert "TSLA" in result

    def test_remove_ticker_absent_from_step(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.remove_ticker("GOOGL")
        result = sim.step()
        assert "GOOGL" not in result
        assert "AAPL" in result

    def test_add_duplicate_is_noop(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("AAPL")
        assert len(sim.get_tickers()) == 1

    def test_remove_nonexistent_is_noop(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.remove_ticker("ZZZZ")   # Must not raise

    def test_unknown_ticker_gets_random_seed_price(self):
        sim = GBMSimulator(tickers=["ZZZZ"])
        price = sim.get_price("ZZZZ")
        assert 50.0 <= price <= 300.0

    def test_empty_step_returns_empty_dict(self):
        sim = GBMSimulator(tickers=[])
        assert sim.step() == {}

    def test_cholesky_none_for_single_ticker(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None

    def test_cholesky_built_for_two_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        assert sim._cholesky is not None

    def test_cholesky_rebuilt_on_add(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None
        sim.add_ticker("GOOGL")
        assert sim._cholesky is not None

    def test_full_default_watchlist_cholesky(self):
        """Correlation matrix for all 10 default tickers must be valid."""
        tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]
        sim = GBMSimulator(tickers=tickers)
        assert sim._cholesky is not None
        result = sim.step()
        assert len(result) == 10
```

### 12.4 `test_simulator_source.py` — SimulatorDataSource (integration)

```python
import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:

    async def test_start_seeds_cache_immediately(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])

        # Cache populated before first loop tick
        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None

        await source.stop()

    async def test_prices_update_over_time(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await source.start(["AAPL"])

        v0 = cache.version
        await asyncio.sleep(0.3)   # several ticks
        assert cache.version > v0

        await source.stop()

    async def test_stop_is_idempotent(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache)
        await source.start(["AAPL"])
        await source.stop()
        await source.stop()   # Must not raise

    async def test_add_and_remove_ticker(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])

        await source.add_ticker("TSLA")
        assert "TSLA" in source.get_tickers()
        assert cache.get("TSLA") is not None

        await source.remove_ticker("TSLA")
        assert "TSLA" not in source.get_tickers()
        assert cache.get("TSLA") is None

        await source.stop()
```

### 12.5 `test_factory.py` — Factory

```python
import pytest
from unittest.mock import patch
from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.simulator import SimulatorDataSource


def test_no_api_key_returns_simulator():
    cache = PriceCache()
    with patch.dict("os.environ", {}, clear=True):
        source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)


def test_empty_api_key_returns_simulator():
    cache = PriceCache()
    with patch.dict("os.environ", {"MASSIVE_API_KEY": ""}):
        source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)


def test_whitespace_api_key_returns_simulator():
    cache = PriceCache()
    with patch.dict("os.environ", {"MASSIVE_API_KEY": "   "}):
        source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)
```

---

## 13. Error Handling & Edge Cases

### Empty watchlist at startup

If the database watchlist is empty, `source.start([])` is called. Both the
simulator and the cache begin with no tickers. The SSE endpoint sends no data
events (only the initial `retry` directive). When the user adds a ticker, the
source starts tracking it and the SSE stream begins populating.

### Price cache miss during a trade

The simulator seeds the cache inside `add_ticker()`, so a price is always
available for simulator-backed tickers. However, a robust route handler should
still guard:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker!r}. Please wait a moment.",
    )
```

### Simulator exception in `_run_loop`

The loop catches all exceptions and continues:

```python
try:
    prices = self._sim.step()
    ...
except Exception:
    logger.exception("Simulator step failed")
await asyncio.sleep(self._interval)
```

A single bad tick (e.g., a `numpy` underflow in a pathological scenario) will
not kill the data feed.

### Cholesky failure on bad correlation matrix

`np.linalg.cholesky` raises `LinAlgError` if the matrix is not positive
definite. In practice this cannot happen with the defined correlation values
(all in (0, 1), diagonal = 1), but it would surface immediately at startup or
on `add_ticker` — never silently. This is the correct behaviour; treat it as a
programming error.

### Thread safety

`PriceCache` uses `threading.Lock`. Under normal load (10 tickers, 2 ticks/s,
a few SSE readers) lock contention is negligible. The critical section is a
dict lookup and assignment — microseconds at most.

---

## 14. Configuration Summary

All tunable parameters and their defaults:

| Parameter | Location | Default | Effect |
|---|---|---|---|
| `MASSIVE_API_KEY` | Environment variable | `""` | If set, Massive API is used instead of simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5 s` | Time between simulator ticks |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Probability of a random shock per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step (fraction of a trading year) |
| SSE push interval | `_generate_events()` | `0.5 s` | How often the SSE generator polls the cache |
| SSE retry directive | `_generate_events()` yield | `1000 ms` | Browser reconnect delay after disconnect |

### Derived quantities at defaults

| Quantity | Value |
|---|---|
| Ticks per second | 2 |
| Per-tick vol (sigma=0.25) | ≈ 0.0073% |
| Expected daily vol (sigma=0.25) | ≈ 1.57% |
| Expected shock frequency (10 tickers) | ≈ 1 per 50 s |
| SSE events per second per client | 2 (if prices changed) |
