# FinAlly - AI Trading Workstation

An AI-powered trading workstation that streams live market data, manages a simulated portfolio, and integrates an LLM chat assistant capable of analyzing positions and executing trades.

Built as a capstone project for an agentic AI coding course, demonstrating how orchestrated AI agents produce a production-quality full-stack application.

## Features

- Live price streaming via SSE with green/red flash animations
- Sparkline mini-charts per ticker, populated from the live stream
- Click any ticker to open a detailed chart
- Buy and sell shares with instant market-order fills
- Portfolio heatmap (treemap) colored by P&L
- P&L line chart tracking total portfolio value over time
- Positions table with unrealized P&L per holding
- AI chat assistant that analyzes your portfolio and executes trades by natural language

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js (TypeScript), static export, Tailwind CSS |
| Backend | FastAPI (Python), managed with `uv` |
| Database | SQLite, lazy-initialized on first run |
| Real-time | Server-Sent Events (SSE) |
| AI | OpenAI SDK (`gpt-4o`), structured outputs |
| Deployment | Single Docker container, port 8000 |

## Quick Start

**Prerequisites**: Docker installed and running.

1. Copy `.env.example` to `.env` and set your OpenRouter API key:

   ```
   OPENAI_API_KEY=your-key-here
   ```

2. Run the start script:

   **macOS/Linux**
   ```bash
   ./scripts/start_mac.sh
   ```

   **Windows**
   ```powershell
   .\scripts\start_windows.ps1
   ```

3. Open `http://localhost:8000` in your browser.

To stop:
```bash
./scripts/stop_mac.sh        # macOS/Linux
.\scripts\stop_windows.ps1   # Windows
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | Yes | OpenRouter API key for LLM chat |
| `MASSIVE_API_KEY` | No | Polygon.io key for real market data (simulator used if absent) |
| `LLM_MOCK` | No | Set to `true` for deterministic mock responses (E2E testing) |

## Default State

- Starting cash: $10,000
- Default watchlist: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX
- Market data: GBM-based simulator at ~500ms intervals (real data via `MASSIVE_API_KEY`)

## Architecture

```
port 8000
  FastAPI
  ├── /api/*          REST endpoints
  ├── /api/stream/*   SSE price stream
  └── /*              Next.js static export
  SQLite (volume-mounted at db/finally.db)
  Background task: market data simulator
```

Data persists across restarts via a named Docker volume. No login or setup required.

## Running Tests

E2E tests use Playwright against a Docker Compose stack with `LLM_MOCK=true`:

```bash
cd test
docker compose -f docker-compose.test.yml up --abort-on-container-exit
```

Unit tests (backend):

```bash
cd backend
uv run pytest
```
