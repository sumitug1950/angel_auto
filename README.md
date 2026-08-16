# angel_auto

Advanced automated Nifty options trading system on Angel One SmartAPI. Manually-directed
(Long/Short), MACD-confirmed entries, ITM/OTM margin-efficient spreads, fixed-risk exits.

Full design/spec lives in the project plan (see the conversation this was built from, or ask
to have it re-summarized) - this README just covers running what's built so far.

## Status

Phase 0 (scaffold) complete: project structure, config schema, logging. No trading logic yet.

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

Not yet available - broker auth and market data (Phase 1) come next.
