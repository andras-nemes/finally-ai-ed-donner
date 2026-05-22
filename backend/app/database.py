"""SQLite data access layer for FinAlly.

All CRUD functions take a sqlite3.Connection as their first argument.
Use `get_db()` as a context manager to obtain a connection with auto-commit/rollback.
Use `init_db()` on startup to create schema and seed default data.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

DEFAULT_SEED_TICKERS = [
    "AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
    "NVDA", "META", "JPM", "V", "NFLX",
]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users_profile (
    id TEXT PRIMARY KEY,
    cash_balance REAL NOT NULL DEFAULT 10000.0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    id TEXT PRIMARY KEY,
    ticker TEXT UNIQUE NOT NULL,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    ticker TEXT PRIMARY KEY,
    quantity REAL NOT NULL,
    avg_cost REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    executed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id TEXT PRIMARY KEY,
    total_value REAL NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    actions TEXT,
    created_at TEXT NOT NULL
);
"""


def _default_db_path() -> str:
    env = os.environ.get("DB_PATH", "")
    if env:
        return env
    return str(Path(__file__).parent.parent.parent / "db" / "finally.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Connection context manager
# ---------------------------------------------------------------------------

@contextmanager
def get_db(db_path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that opens a sqlite3 connection.

    Commits on success, rolls back on exception, always closes.
    Sets row_factory to sqlite3.Row for dict-like row access.
    Creates parent directories if they don't exist.
    """
    path = db_path or _default_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Database initialization
# ---------------------------------------------------------------------------

def init_db(db_path: str | None = None) -> None:
    """Create schema and seed default data if empty.

    Idempotent — safe to call on every startup.
    """
    path = db_path or _default_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    with get_db(path) as conn:
        conn.executescript(SCHEMA_SQL)

        # Seed only if users_profile is empty
        row = conn.execute("SELECT COUNT(*) FROM users_profile").fetchone()
        if row[0] == 0:
            _seed_data(conn)
            logger.info("Database seeded with default data at %s", path)
        else:
            logger.info("Database already initialized at %s", path)


def _seed_data(conn: sqlite3.Connection) -> None:
    """Insert default user profile and watchlist."""
    now = _now_iso()

    conn.execute(
        "INSERT INTO users_profile (id, cash_balance, created_at) VALUES (?, ?, ?)",
        ("default", 10000.0, now),
    )

    for ticker in DEFAULT_SEED_TICKERS:
        conn.execute(
            "INSERT INTO watchlist (id, ticker, added_at) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), ticker, now),
        )


# ---------------------------------------------------------------------------
# User profile
# ---------------------------------------------------------------------------

def get_user_profile(conn: sqlite3.Connection) -> dict:
    """Return the single user profile as a dict."""
    row = conn.execute("SELECT * FROM users_profile WHERE id = 'default'").fetchone()
    if row is None:
        raise RuntimeError("User profile not found — was the database seeded?")
    return dict(row)


def update_cash_balance(conn: sqlite3.Connection, delta: float) -> float:
    """Adjust cash balance by `delta` (positive = add, negative = subtract).

    Returns the new cash balance.
    Raises ValueError if the resulting balance would be negative.
    """
    row = conn.execute(
        "SELECT cash_balance FROM users_profile WHERE id = 'default'"
    ).fetchone()
    if row is None:
        raise RuntimeError("User profile not found")

    new_balance = row["cash_balance"] + delta
    if new_balance < 0:
        raise ValueError(
            f"Insufficient funds: balance {row['cash_balance']:.2f}, delta {delta:.2f}"
        )

    conn.execute(
        "UPDATE users_profile SET cash_balance = ? WHERE id = 'default'",
        (new_balance,),
    )
    return new_balance


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def get_watchlist(conn: sqlite3.Connection) -> list[dict]:
    """Return all watchlist rows as a list of dicts."""
    rows = conn.execute(
        "SELECT id, ticker, added_at FROM watchlist ORDER BY added_at"
    ).fetchall()
    return [dict(row) for row in rows]


def get_watchlist_tickers(conn: sqlite3.Connection) -> list[str]:
    """Return just the ticker symbols from the watchlist."""
    rows = conn.execute("SELECT ticker FROM watchlist ORDER BY added_at").fetchall()
    return [row["ticker"] for row in rows]


def add_to_watchlist(conn: sqlite3.Connection, ticker: str) -> dict:
    """Add a ticker to the watchlist.

    Returns the new row as a dict.
    Raises ValueError if the ticker already exists.
    """
    existing = conn.execute(
        "SELECT id FROM watchlist WHERE ticker = ?", (ticker,)
    ).fetchone()
    if existing is not None:
        raise ValueError(f"Ticker {ticker!r} is already in the watchlist")

    row_id = str(uuid.uuid4())
    now = _now_iso()
    conn.execute(
        "INSERT INTO watchlist (id, ticker, added_at) VALUES (?, ?, ?)",
        (row_id, ticker, now),
    )
    return {"id": row_id, "ticker": ticker, "added_at": now}


def remove_from_watchlist(conn: sqlite3.Connection, ticker: str) -> None:
    """Remove a ticker from the watchlist.

    Raises ValueError if the ticker is not in the watchlist.
    """
    result = conn.execute(
        "DELETE FROM watchlist WHERE ticker = ?", (ticker,)
    )
    if result.rowcount == 0:
        raise ValueError(f"Ticker {ticker!r} is not in the watchlist")


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def get_positions(conn: sqlite3.Connection) -> list[dict]:
    """Return all open positions as a list of dicts."""
    rows = conn.execute(
        "SELECT ticker, quantity, avg_cost, updated_at FROM positions ORDER BY ticker"
    ).fetchall()
    return [dict(row) for row in rows]


def get_position(conn: sqlite3.Connection, ticker: str) -> dict | None:
    """Return the position for a single ticker, or None if not held."""
    row = conn.execute(
        "SELECT ticker, quantity, avg_cost, updated_at FROM positions WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    return dict(row) if row else None


def upsert_position(
    conn: sqlite3.Connection,
    ticker: str,
    quantity: float,
    avg_cost: float,
) -> None:
    """Insert or update a position.

    If quantity <= 0, the position is deleted (position closed).
    """
    if quantity <= 0:
        conn.execute("DELETE FROM positions WHERE ticker = ?", (ticker,))
        return

    now = _now_iso()
    conn.execute(
        """
        INSERT INTO positions (ticker, quantity, avg_cost, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            quantity = excluded.quantity,
            avg_cost = excluded.avg_cost,
            updated_at = excluded.updated_at
        """,
        (ticker, quantity, avg_cost, now),
    )


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

def record_trade(
    conn: sqlite3.Connection,
    ticker: str,
    side: str,
    quantity: float,
    price: float,
) -> dict:
    """Insert a trade record and return it as a dict."""
    trade_id = str(uuid.uuid4())
    now = _now_iso()
    conn.execute(
        "INSERT INTO trades (id, ticker, side, quantity, price, executed_at) VALUES (?, ?, ?, ?, ?, ?)",
        (trade_id, ticker, side, quantity, price, now),
    )
    return {
        "id": trade_id,
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "price": price,
        "executed_at": now,
    }


# ---------------------------------------------------------------------------
# Portfolio snapshots
# ---------------------------------------------------------------------------

def get_portfolio_history(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Return the most recent `limit` portfolio snapshots, oldest first."""
    rows = conn.execute(
        """
        SELECT total_value, recorded_at FROM (
            SELECT total_value, recorded_at
            FROM portfolio_snapshots
            ORDER BY recorded_at DESC
            LIMIT ?
        ) ORDER BY recorded_at ASC
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def record_portfolio_snapshot(conn: sqlite3.Connection, total_value: float) -> None:
    """Insert a portfolio value snapshot."""
    conn.execute(
        "INSERT INTO portfolio_snapshots (id, total_value, recorded_at) VALUES (?, ?, ?)",
        (str(uuid.uuid4()), total_value, _now_iso()),
    )


# ---------------------------------------------------------------------------
# Chat messages
# ---------------------------------------------------------------------------

def get_chat_history(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Return the most recent `limit` chat messages, oldest first."""
    rows = conn.execute(
        """
        SELECT id, role, content, actions, created_at FROM (
            SELECT id, role, content, actions, created_at
            FROM chat_messages
            ORDER BY created_at DESC
            LIMIT ?
        ) ORDER BY created_at ASC
        """,
        (limit,),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        if d["actions"] is not None:
            try:
                d["actions"] = json.loads(d["actions"])
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(d)
    return result


def record_chat_message(
    conn: sqlite3.Connection,
    role: str,
    content: str,
    actions: dict | None = None,
) -> dict:
    """Insert a chat message and return it as a dict."""
    msg_id = str(uuid.uuid4())
    now = _now_iso()
    actions_json = json.dumps(actions) if actions is not None else None
    conn.execute(
        "INSERT INTO chat_messages (id, role, content, actions, created_at) VALUES (?, ?, ?, ?, ?)",
        (msg_id, role, content, actions_json, now),
    )
    return {
        "id": msg_id,
        "role": role,
        "content": content,
        "actions": actions,
        "created_at": now,
    }
