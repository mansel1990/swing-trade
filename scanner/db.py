import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

SCHEMA = os.environ.get("DB_SCHEMA", "swing")

# Shared column definition — both tables use the same schema.
# breakout_level stores the resistance high (breakout) or 20 EMA level (ema pullback).
_COLUMNS = """
    id              SERIAL PRIMARY KEY,
    date            DATE NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    company_name    VARCHAR(100),
    cmp             NUMERIC(10,2),
    breakout_level  NUMERIC(10,2),
    entry_min       NUMERIC(10,2),
    entry_max       NUMERIC(10,2),
    target          NUMERIC(10,2),
    stop_loss       NUMERIC(10,2),
    volume_ratio    NUMERIC(5,2),
    rsi             NUMERIC(5,2),
    signal_strength VARCHAR(10),
    created_at      TIMESTAMP DEFAULT NOW()
"""


def get_connection():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def ensure_table(table: str = "breakout_signals"):
    """Create the schema + named table if they don't exist yet."""
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};")
            cur.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA}.{table} ({_COLUMNS});")
    conn.close()
    print(f"Table '{SCHEMA}.{table}' is ready.")


def delete_today_signals(table: str = "breakout_signals"):
    """Remove today's rows before re-inserting (makes runs idempotent)."""
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {SCHEMA}.{table} WHERE date = CURRENT_DATE;")
    conn.close()


def save_signals(signals: list[dict], table: str = "breakout_signals"):
    if not signals:
        return
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            for s in signals:
                cur.execute(
                    f"""
                    INSERT INTO {SCHEMA}.{table}
                        (date, symbol, company_name, cmp, breakout_level,
                         entry_min, entry_max, target, stop_loss,
                         volume_ratio, rsi, signal_strength)
                    VALUES
                        (CURRENT_DATE, %(symbol)s, %(company_name)s, %(cmp)s,
                         %(breakout_level)s, %(entry_min)s, %(entry_max)s,
                         %(target)s, %(stop_loss)s, %(volume_ratio)s,
                         %(rsi)s, %(signal_strength)s)
                    """,
                    s,
                )
    conn.close()
    print(f"Saved {len(signals)} signal(s) to {SCHEMA}.{table}.")
