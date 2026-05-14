# Database — Neon Postgres

## Connection
```
Provider: Neon serverless Postgres
Schema:   swing  (separate from the existing "stocks" schema — do not touch "stocks")
Env var:  DATABASE_URL  (in scanner/.env and Railway env)
          DB_SCHEMA=swing
```

## Tables

All 5 signal tables share the same column definition (`_COLUMNS` in `db.py`).

### Signal tables
| Table | Strategy | Filled by |
|---|---|---|
| swing.signals | Breakout | main.py → db.save_signals() |
| swing.ema_signals | EMA Pullback | main.py → db.save_signals() |
| swing.vcp_signals | VCP | main.py → db.save_signals() |
| swing.rs_signals | RS Resilience | main.py → db.save_signals() |
| swing.mean_reversion_signals | Mean Reversion | main.py → db.save_signals() |

### Shared column schema (all signal tables)
```sql
CREATE TABLE IF NOT EXISTS swing.<table> (
    id              SERIAL PRIMARY KEY,
    date            DATE NOT NULL,          -- scanner run date
    symbol          VARCHAR(20) NOT NULL,
    company_name    VARCHAR(100),
    cmp             NUMERIC(10,2),          -- price at scan time
    breakout_level  NUMERIC(10,2),          -- strategy-specific reference
    entry_min       NUMERIC(10,2),
    entry_max       NUMERIC(10,2),
    target          NUMERIC(10,2),
    stop_loss       NUMERIC(10,2),
    volume_ratio    NUMERIC(5,2),
    rsi             NUMERIC(5,2),
    signal_strength VARCHAR(10),            -- "Strong" | "Moderate"
    created_at      TIMESTAMP DEFAULT NOW()
);
```

### swing.strategy_performance
Tracks every signal as a simulated ₹10,000 position from signal_date to exit.

```sql
CREATE TABLE IF NOT EXISTS swing.strategy_performance (
    id               SERIAL PRIMARY KEY,
    signal_date      DATE NOT NULL,
    strategy         VARCHAR(50),           -- "breakout" | "ema_pullback" | "vcp" | "rs_resilience" | "mean_reversion"
    symbol           VARCHAR(20) NOT NULL,
    entry_price      NUMERIC(10,2),
    target_price     NUMERIC(10,2),
    stop_loss_price  NUMERIC(10,2),
    investment       NUMERIC(10,2),         -- always 10000
    exit_date        DATE,
    exit_price       NUMERIC(10,2),
    exit_reason      VARCHAR(20),           -- "target_hit" | "stop_loss" | "timeout"
    pnl              NUMERIC(10,2),
    pnl_pct          NUMERIC(8,4),
    status           VARCHAR(10)            -- "open" | "closed"
);
```

## DB helpers (`scanner/db.py`)

```python
get_connection()                         # Returns psycopg2 connection
ensure_table(table: str)                 # CREATE TABLE IF NOT EXISTS swing.<table>
delete_today_signals(table: str)         # DELETE WHERE date = CURRENT_DATE (idempotent)
save_signals(signals: list[dict], table) # Batch INSERT into swing.<table>
```

All functions accept `table` as a parameter — same helpers work for all 5 signal tables.

## Performance helpers (`scanner/performance.py`)

```python
evaluate_open_positions()
# - Fetches all rows where status = 'open'
# - Downloads today's OHLC for those symbols
# - Closes positions: target_hit / stop_loss / timeout (7 days)
# - Updates pnl, pnl_pct, exit_date, exit_price, exit_reason, status

log_new_signals(signals: list[dict], strategy: str)
# - Inserts each signal as a new 'open' row in strategy_performance
# - Skips if (signal_date, symbol, strategy) already exists (idempotent)
```

## How data flows each evening
1. `evaluate_open_positions()` — closes anything that hit target/SL/timeout since last run
2. `delete_today_signals(table)` → `save_signals(signals, table)` for each strategy
3. `log_new_signals(signals, strategy)` — inserts fresh open positions
