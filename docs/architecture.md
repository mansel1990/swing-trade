# Scanner Architecture

## Directory Structure
```
scanner/
├── main.py                    # Orchestrator — runs all strategies, saves to DB
├── indicators.py              # Pure indicator math (RSI, EMA, volume, contractions, RS)
├── ema_scanner.py             # EMA Pullback strategy module
├── vcp_scanner.py             # VCP strategy module
├── rs_scanner.py              # Relative Strength Resilience strategy module
├── mean_reversion_scanner.py  # Mean Reversion strategy module
├── db.py                      # Neon Postgres helpers (parameterized by table name)
├── performance.py             # Position tracker: open → target/SL/timeout → closed
├── stocks.py                  # 180 NSE tickers + BATCH_SIZE=50
└── .env                       # DATABASE_URL, DB_SCHEMA=swing
```

## Data Flow

```
main.py (evening run)
  │
  ├─ fetch_index("^NSEI")          # one-shot Nifty fetch (for RS strategy)
  │
  ├─ for each batch of 50 stocks:
  │     yf.download(batch, period=220d, interval=1d, group_by=ticker)
  │     │
  │     └─ for each symbol in batch:
  │           analyse_breakout(sym, df)       → dict | None
  │           analyse_ema_pullback(sym, df)   → dict | None
  │           analyse_vcp(sym, df)            → dict | None
  │           analyse_rs_resilience(sym, df, nifty_df) → dict | None
  │           analyse_mean_reversion(sym, df) → dict | None
  │
  ├─ sort each results list by volume_ratio DESC
  ├─ trim to TOP_N = 5
  ├─ print_section() for each strategy
  │
  └─ if --save:
        performance.evaluate_open_positions()    # close target/SL/timeout hits
        for each strategy:
            db.ensure_table(table)               # CREATE TABLE IF NOT EXISTS
            db.delete_today_signals(table)       # idempotent — safe to re-run
            db.save_signals(results, table)
            performance.log_new_signals(results, strategy_name)
```

## Key Design Decisions

### Single batch download for all strategies
yfinance is rate-limited. All 5 strategies share the same OHLCV download — zero extra API calls for adding a new strategy (except RS which needs `^NSEI` once).

### LOOKBACK_DAYS = 220
VCP requires a 200-day EMA and 52-week stats. This bumped the lookback from the original 60 days. All other strategies benefit from the extra history too.

### TOP_N = 5
Disciplined trading: too many signals cause confusion and over-trading. Top 5 by volume ratio (strongest conviction) per strategy per day.

### Idempotent saves
`delete_today_signals()` before each `save_signals()` means re-running the scanner on the same day is safe. No duplicate signals accumulate.

### Shared table schema
All 5 signal tables use the same column set (see database.md). The `breakout_level` column stores different things per strategy (resistance high, EMA value, pivot, support level) — the dashboard shows whatever label the strategy page provides.

### Performance tracking is strategy-agnostic
`performance.py` doesn't know which strategy it's tracking. `log_new_signals(signals, strategy_name)` inserts with a free-text strategy column. `evaluate_open_positions()` fetches all open rows regardless of strategy and applies the same target/SL/timeout logic.
