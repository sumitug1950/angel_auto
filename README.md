# angel_auto

Advanced automated Nifty options trading system on Angel One SmartAPI. Manually-directed
(Long/Short), MACD-confirmed entries, ITM/OTM margin-efficient spreads, fixed-risk exits.

Full design/spec lives in the project plan (see the conversation this was built from, or ask
to have it re-summarized).

## Status

All of Phase 0-10 built and tested (159 automated tests + repeated live verification against
the real Angel One account): scaffold, broker auth/WebSocket, analytics (Black-Scholes/MACD/
IV Rank), persistence, strategy engine, risk management, OMS + paper broker, scheduler,
dashboard, backtesting, and the live broker adapter (present but deliberately not armed -
see **Going live** below). Docker/deployment (Phase 11) not yet done.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item config\.env.example config\.env
# edit config\.env with your real Angel One API key / client code / mpin / totp secret
```

`config/.env` is gitignored - never commit real credentials. `config/config.yaml` and
`config/strategies.yaml` hold everything else (risk limits, strategy tunables) and are safe
to version-control.

## Config at a glance

- `config/config.yaml` - mode (paper/live/backtest), risk limits, square-off times, DB/logging.
- `config/strategies.yaml` - the active strategy's tunables (MACD periods, delta targets, SL/target/trail amounts, sizing).

## Running

```powershell
# run the dashboard (main way to operate the system - logs in, connects WS, starts the
# strategy/OMS/scheduler loop, serves the web UI)
.venv\Scripts\python.exe scripts\run_dashboard.py
# then open http://127.0.0.1:8000

# one-off checks, useful for diagnosing a connection/data problem in isolation
.venv\Scripts\python.exe scripts\dry_run_auth.py       # just TOTP login
.venv\Scripts\python.exe scripts\dry_run_ws.py 30       # just the live tick feed
.venv\Scripts\python.exe scripts\dry_run_app.py 30      # the full app, no dashboard

# backtest over a historical date range (daily bars - see angel_auto/backtest/engine.py
# docstring for exactly what that does and doesn't validate)
.venv\Scripts\python.exe scripts\run_backtest.py 2026-03-25 2026-08-14
```

Run tests with `.venv\Scripts\python.exe -m pytest tests\ -v`.

## Going live

`mode: paper` in `config.yaml` is the only mode that should be used until the strategy has
been validated running in paper mode for a real stretch of time (see the plan's pre-live
checklist - minimum weeks, not days, no code changes during the window).

When actually ready, `mode: live` in config.yaml is *not* enough on its own - `TradingApp`
also requires the environment variable `ANGEL_LIVE_TRADING_CONFIRMED=YES_I_UNDERSTAND_THE_RISK`
to be set by hand in the shell before it will start (never put this in `.env` or any
committed file). This is a deliberate second barrier so real-money trading can never turn on
from a one-line config edit alone. Even then: start at the smallest possible size, watch it
closely, and don't leave it unattended until you've seen it handle a full trading day.
