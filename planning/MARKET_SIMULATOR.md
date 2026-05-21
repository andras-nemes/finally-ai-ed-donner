# Market Simulator Design

Approach and code structure for simulating realistic stock prices when no `MASSIVE_API_KEY` is configured.

This document uses imaginary tickers as examples:

| Ticker | Company | Sector | Volatility |
|--------|---------|--------|------------|
| FLUX | FluxCorp Technologies | Tech | Medium-high |
| ZEPH | Zephyr Industries | Energy | Medium |
| NOVA | NovaDynamics | Tech | High |
| CRUX | Cruxion Capital | Finance | Low |
| ORBT | OrbitalTech Systems | Tech | High |
| ECHO | Echo Technologies | Tech | Medium |
| MYST | Mysteria Holdings | Finance | Low |
| LMNX | Lumex Corp | Tech | Medium |
| DUSK | Dusk Enterprises | Consumer | Medium |
| VRTX | Vortex Capital | Finance | Low |

## Overview

The simulator uses **Geometric Brownian Motion (GBM)** to generate realistic price paths. GBM is the standard mathematical model underlying Black-Scholes option pricing. Key properties:

- Prices are always positive (the model is multiplicative — uses `exp()`)
- Price changes are proportional to the current price (a $1 move means more at $10 than at $200)
- Accumulated over time, log returns follow a normal distribution (lognormal price distribution)
- Each ticker has its own drift (`mu`) and volatility (`sigma`) parameters

Updates run every ~500ms, producing a continuous stream of price ticks that feels alive.

## GBM Math

At each time step, a price evolves as:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

Where:

| Symbol | Meaning |
|--------|---------|
| `S(t)` | Current price |
| `mu` | Annualized drift (expected return), e.g. 0.05 = 5% |
| `sigma` | Annualized volatility, e.g. 0.25 = 25% |
| `dt` | Time step as fraction of a trading year |
| `Z` | Standard normal random variable drawn from N(0,1) |

The Ito correction term (`-sigma^2/2`) ensures the expected price growth is `mu` and not `mu + sigma^2/2`. Without it, the model would systematically drift upward faster than intended.

### Choosing `dt`

For 500ms updates with approximately 252 trading days and 6.5 trading hours per day:

```
trading seconds per year = 252 * 6.5 * 3600 = 5,896,800

dt = 0.5 / 5,896,800 ≈ 8.48e-8
```

This tiny `dt` produces sub-cent price moves per tick, which accumulate naturally into realistic intraday ranges over time.

### Verification

With `sigma=0.25` and `dt=8.48e-8`, the per-step standard deviation is:
```
sigma * sqrt(dt) = 0.25 * sqrt(8.48e-8) ≈ 0.000073  (0.0073%)
```

Over a full simulated trading day (~46,800 ticks at 500ms):
```
daily vol ≈ sigma * sqrt(1/252) = 0.25 / sqrt(252) ≈ 1.57%
```

That matches a typical mid-cap tech stock's daily volatility — realistic.

## Correlated Moves

Real stocks in the same sector tend to move together. Tech stocks broadly rise and fall with market sentiment. We use **Cholesky decomposition** to generate correlated random variables.

### The Math

Given a correlation matrix `C` (symmetric, positive semi-definite), compute its Cholesky factor `L` such that:
```
L @ L.T = C
```

Then, for a vector of independent standard normals `Z_independent`:
```
Z_correlated = L @ Z_independent
```

`Z_correlated` has the desired inter-variable correlations.

### Correlation Groups (Imaginary Tickers)

```python
CORRELATION_GROUPS = {
    "tech":    {"FLUX", "NOVA", "ORBT", "ECHO", "LMNX"},
    "finance": {"CRUX", "MYST", "VRTX"},
    "energy":  {"ZEPH"},
    "consumer":{"DUSK"},
}

INTRA_TECH_CORR    = 0.6   # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR   = 0.3   # Cross-sector baseline
NOVA_CORR          = 0.3   # NOVA is volatile and does its own thing
```

Pairwise correlation lookup:

```python
def _pairwise_correlation(t1: str, t2: str) -> float:
    tech    = CORRELATION_GROUPS["tech"]
    finance = CORRELATION_GROUPS["finance"]

    # NOVA is high-vol and decorrelated from its sector peers
    if t1 == "NOVA" or t2 == "NOVA":
        return NOVA_CORR

    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR

    return CROSS_GROUP_CORR
```

The correlation matrix is rebuilt whenever tickers are added or removed. With `n < 50` tickers, this is fast enough to do inline.

## Random Shock Events

Every step, each ticker has a small probability of a sudden "event" — a 2-5% move in either direction. This adds drama and keeps the dashboard visually interesting.

```python
EVENT_PROBABILITY = 0.001  # 0.1% chance per ticker per tick

if random.random() < EVENT_PROBABILITY:
    shock_magnitude = random.uniform(0.02, 0.05)  # 2-5%
    shock_sign = random.choice([-1, 1])
    price *= (1 + shock_magnitude * shock_sign)
```

With 10 tickers at 2 ticks/second, expect roughly one event somewhere every 50 seconds — enough to notice.

## Seed Prices

Realistic starting prices for imaginary tickers. These are entirely made up and exist only to give the simulator plausible starting values.

```python
SEED_PRICES: dict[str, float] = {
    "FLUX":  145.00,   # Mid-cap tech
    "ZEPH":   88.00,   # Energy
    "NOVA":  312.00,   # High-growth tech
    "CRUX":   67.00,   # Finance (conservative)
    "ORBT":  230.00,   # Tech infrastructure
    "ECHO":  178.00,   # Tech
    "MYST":   54.00,   # Finance holding
    "LMNX":   95.00,   # Tech hardware
    "DUSK":  120.00,   # Consumer
    "VRTX":   81.00,   # Finance
}
```

Tickers added dynamically (not in the seed list) start at a random price between $50 and $300.

## Per-Ticker Parameters

Each ticker has its own `sigma` (volatility) and `mu` (drift) to reflect its character:

```python
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "FLUX": {"sigma": 0.28, "mu": 0.06},   # Moderate-high vol tech
    "ZEPH": {"sigma": 0.22, "mu": 0.04},   # Energy, cyclical
    "NOVA": {"sigma": 0.55, "mu": 0.08},   # High vol, strong growth story
    "CRUX": {"sigma": 0.15, "mu": 0.03},   # Low vol finance
    "ORBT": {"sigma": 0.35, "mu": 0.07},   # High vol infrastructure play
    "ECHO": {"sigma": 0.25, "mu": 0.05},   # Standard tech
    "MYST": {"sigma": 0.14, "mu": 0.03},   # Very low vol holding company
    "LMNX": {"sigma": 0.23, "mu": 0.05},   # Tech hardware
    "DUSK": {"sigma": 0.20, "mu": 0.04},   # Consumer staples
    "VRTX": {"sigma": 0.16, "mu": 0.03},   # Finance
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}
```

## Implementation

```python
import math
import random
import logging

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    NOVA_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Correlated GBM price simulator for multiple imaginary tickers.

    Math: S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)
    """

    # 500ms as a fraction of one trading year
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600   # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR    # ~8.48e-8

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

        Hot path — called every 500ms. Keep it fast.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1 + shock)
                logger.debug("Event on %s: %.1f%%", ticker, shock * 100)

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

    def _add_ticker_internal(self, ticker: str) -> None:
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

        if t1 == "NOVA" or t2 == "NOVA":
            return NOVA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

## SimulatorDataSource (Async Wrapper)

`SimulatorDataSource` is the `MarketDataSource` implementation that drives `GBMSimulator` from an asyncio background task.

```python
import asyncio

class SimulatorDataSource(MarketDataSource):
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
        # Seed cache so SSE has data immediately on first connect
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

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
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

## File Structure

```
backend/
  app/
    market/
      simulator.py    # GBMSimulator + SimulatorDataSource
      seed_prices.py  # SEED_PRICES, TICKER_PARAMS, correlation constants
```

`seed_prices.py` holds only constants. `simulator.py` holds the two classes.

## Behavioral Notes

- **Prices never go negative**: GBM uses `exp()` which is always positive — this is a hard mathematical guarantee, not a runtime check
- **Tiny per-tick moves accumulate naturally**: sub-cent changes at 2 ticks/second sum to realistic intraday ranges
- **Cholesky requirement**: the correlation matrix must be positive semi-definite. A matrix built from valid pairwise correlations (all between -1 and 1, diagonal = 1) always satisfies this. `np.linalg.cholesky` will raise `LinAlgError` if not — treat this as a programming error, not a runtime condition
- **Adding tickers mid-session**: `_rebuild_cholesky()` is O(n^2) but with `n < 50` tickers this runs in microseconds
- **NOVA as an outlier**: by giving NOVA a low correlation (0.3) with its tech peers and high sigma (0.55), it behaves like a volatile speculative name — its price path diverges visibly from the other tech names
- **Event frequency**: with 10 tickers at probability 0.001 per tick and 2 ticks/second, expect roughly one event somewhere every 50 seconds — enough to keep the UI interesting without being distracting
