# Massive API Reference

Reference documentation for the Massive (formerly Polygon.io) REST API as used in FinAlly.

This document uses **imaginary tickers** (FLUX, ZEPH, NOVA, CRUX, ORBT) to illustrate API usage. FinAlly defaults to the built-in GBM simulator; the Massive integration activates only when `MASSIVE_API_KEY` is set.

## Overview

- **Base URL**: `https://api.massive.com` (legacy `https://api.polygon.io` also works)
- **Python package**: `massive` (`uv add massive` or `pip install -U massive`)
- **Min Python**: 3.9+
- **Auth**: API key passed to `RESTClient(api_key=...)` or read automatically from `MASSIVE_API_KEY` env var
- **Auth mechanism**: `Authorization: Bearer <API_KEY>` header (handled by the client)

## Imaginary Tickers Used in Examples

| Ticker | Company | Sector |
|--------|---------|--------|
| FLUX | FluxCorp Technologies | Tech |
| ZEPH | Zephyr Industries | Energy |
| NOVA | NovaDynamics | Tech |
| CRUX | Cruxion Capital | Finance |
| ORBT | OrbitalTech Systems | Tech |

These are entirely fictional and exist only to demonstrate API patterns.

## Rate Limits

| Tier | Limit |
|------|-------|
| Free | 5 requests/minute |
| Paid | Effectively unlimited (stay under 100 req/s) |

For FinAlly: free tier polls every 15 seconds; paid tiers can poll every 2-5 seconds.

## Client Initialization

```python
from massive import RESTClient

# Reads MASSIVE_API_KEY from environment automatically
client = RESTClient()

# Or pass the key directly
client = RESTClient(api_key="your_key_here")
```

## Endpoints Used in FinAlly

### 1. Snapshot — All Tickers (Primary Endpoint)

Gets current prices for multiple tickers in a single API call. This is the main polling endpoint.

**REST**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=FLUX,ZEPH,NOVA`

**Python**:
```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient()

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["FLUX", "ZEPH", "NOVA", "CRUX", "ORBT"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price:.2f}")
    print(f"  Day change: {snap.day.change_percent:.2f}%")
    print(f"  OHLC: O={snap.day.open} H={snap.day.high} L={snap.day.low} C={snap.day.close}")
    print(f"  Volume: {snap.day.volume:,}")
```

**Response structure** (per ticker):
```json
{
  "ticker": "FLUX",
  "day": {
    "open": 142.30,
    "high": 148.75,
    "low": 139.10,
    "close": 145.60,
    "volume": 3200000,
    "volume_weighted_average_price": 143.85,
    "previous_close": 141.20,
    "change": 4.40,
    "change_percent": 3.12
  },
  "last_trade": {
    "price": 145.60,
    "size": 200,
    "exchange": "XNAS",
    "timestamp": 1675190399000
  },
  "last_quote": {
    "bid_price": 145.58,
    "ask_price": 145.62,
    "bid_size": 300,
    "ask_size": 400,
    "spread": 0.04,
    "timestamp": 1675190399500
  }
}
```

**Fields we extract**:
- `last_trade.price` — current price for trading and display
- `day.previous_close` — baseline for day change calculation
- `day.change_percent` — day percentage change
- `last_trade.timestamp` — Unix milliseconds, converted to seconds for storage

### 2. Single Ticker Snapshot

For detailed data on one ticker (e.g., when the user clicks a ticker for the main chart view).

```python
snapshot = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="NOVA",
)

print(f"Price: ${snapshot.last_trade.price:.2f}")
print(f"Bid/Ask: ${snapshot.last_quote.bid_price} / ${snapshot.last_quote.ask_price}")
print(f"Day range: ${snapshot.day.low} - ${snapshot.day.high}")
```

### 3. Previous Close

Gets the previous trading day's OHLC for a ticker. Useful for seeding simulator starting prices.

**REST**: `GET /v2/aggs/ticker/{ticker}/prev`

```python
prev = client.get_previous_close_agg(ticker="ZEPH")

for agg in prev:
    print(f"Previous close: ${agg.close:.2f}")
    print(f"OHLC: O={agg.open} H={agg.high} L={agg.low} C={agg.close}")
    print(f"Volume: {agg.volume:,}")
```

**Response**:
```json
{
  "ticker": "ZEPH",
  "results": [
    {
      "o": 88.40,
      "h": 91.20,
      "l": 87.50,
      "c": 90.10,
      "v": 1500000,
      "t": 1672531200000
    }
  ]
}
```

### 4. Aggregates (Historical Bars)

Historical OHLCV bars over a date range. Not needed for live polling but available for adding historical charts later.

```python
aggs = []
for a in client.list_aggs(
    ticker="ORBT",
    multiplier=1,
    timespan="day",
    from_="2024-01-01",
    to="2024-01-31",
    limit=50000,
):
    aggs.append(a)

for a in aggs:
    print(f"t={a.timestamp} O={a.open} H={a.high} L={a.low} C={a.close} V={a.volume}")
```

### 5. Last Trade / Last Quote

```python
# Most recent trade
trade = client.get_last_trade(ticker="CRUX")
print(f"Last trade: ${trade.price:.2f} x {trade.size}")

# Current NBBO quote
quote = client.get_last_quote(ticker="CRUX")
print(f"Bid: ${quote.bid:.2f} x {quote.bid_size}")
print(f"Ask: ${quote.ask:.2f} x {quote.ask_size}")
```

## How FinAlly Uses the API

The `MassiveDataSource` runs as a background asyncio task:

1. Collects tickers from the in-memory tracker
2. Calls `get_snapshot_all()` with those tickers (one API call for all)
3. Extracts `last_trade.price` and `last_trade.timestamp` from each snapshot
4. Writes to the shared `PriceCache`
5. Sleeps for the poll interval, then repeats

```python
import asyncio
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

async def poll_massive(api_key: str, get_tickers, price_cache, interval: float = 15.0):
    client = RESTClient(api_key=api_key)

    while True:
        tickers = get_tickers()
        if tickers:
            # One API call fetches all tickers
            snapshots = await asyncio.to_thread(
                client.get_snapshot_all,
                market_type=SnapshotMarketType.STOCKS,
                tickers=tickers,
            )
            for snap in snapshots:
                price_cache.update(
                    ticker=snap.ticker,
                    price=snap.last_trade.price,
                    timestamp=snap.last_trade.timestamp / 1000,  # ms -> seconds
                )

        await asyncio.sleep(interval)
```

## Error Handling

The client raises exceptions for HTTP errors:

| Status | Cause | Behavior |
|--------|-------|----------|
| 401 | Invalid API key | Log error, loop retries on next interval |
| 403 | Endpoint not in plan | Log error, loop retries |
| 429 | Rate limit exceeded | Log error, loop retries (back off if persistent) |
| 5xx | Server error | Client has 3 built-in retries; then log and loop retries |

FinAlly does not crash on poll failures — errors are logged and the next poll is attempted after `interval` seconds. The `PriceCache` retains stale prices until fresh ones arrive.

## Notes

- `get_snapshot_all()` fetches all requested tickers in **one API call** — critical for staying within rate limits on the free tier
- Timestamps from the API are Unix milliseconds; convert to seconds by dividing by 1000
- Outside market hours, `last_trade.price` reflects the last traded price (may include after-hours)
- The `day` object resets at market open; during pre-market, values may be from the previous session
- The `massive` package is a synchronous client; use `asyncio.to_thread()` to avoid blocking the event loop
