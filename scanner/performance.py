"""
Performance tracker — called by main.py on each scanner run.

On each run it does two things:
  1. Evaluate all open positions: check if target/SL hit, or timeout (7 days), close them.
  2. Log today's new signals as fresh open positions.

Exit logic:
  - target_hit  : next session high  >= target price  → exit at target
  - stop_loss   : next session low   <= stop loss      → exit at stop loss
  - timeout     : position age >= MAX_HOLD_DAYS        → exit at today's close
"""

import sys
from datetime import date
import yfinance as yf
import pandas as pd
from db import get_connection

SCHEMA       = "swing"
TABLE        = "strategy_performance"
INVESTMENT   = 10_000        # Rs per trade (simulated)
MAX_HOLD_DAYS = 7            # force-exit after this many calendar days


# ─────────────────────────────────────────────────────────────────────────────

def _fetch_today_ohlc(symbols: list[str]) -> dict[str, dict]:
    """Fetch today's OHLC for a list of symbols in one call."""
    if not symbols:
        return {}
    tickers_str = " ".join(f"{s}.NS" for s in symbols)
    try:
        raw = yf.download(
            tickers_str, period="2d", interval="1d",
            group_by="ticker", auto_adjust=True, progress=False, threads=True,
        )
        result = {}
        for sym in symbols:
            ticker = f"{sym}.NS"
            try:
                if len(symbols) == 1:
                    df = raw.copy()
                else:
                    if ticker not in raw.columns.get_level_values(0):
                        continue
                    df = raw[ticker].copy()
                if df.empty:
                    continue
                row = df.iloc[-1]
                result[sym] = {
                    "high":  float(row["High"]),
                    "low":   float(row["Low"]),
                    "close": float(row["Close"]),
                }
            except Exception:
                continue
        return result
    except Exception as e:
        print(f"  [performance] OHLC fetch error: {e}", file=sys.stderr)
        return {}


def _get_open_positions(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, symbol, strategy, entry_price, target_price, stop_loss_price, signal_date
            FROM {SCHEMA}.{TABLE}
            WHERE status = 'open'
            ORDER BY signal_date
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _close_position(conn, pos_id: int, exit_price: float, exit_reason: str, exit_date: date):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {SCHEMA}.{TABLE}
            SET exit_date   = %s,
                exit_price  = %s,
                exit_reason = %s,
                pnl         = ROUND((%s - entry_price) / entry_price * investment, 2),
                pnl_pct     = ROUND((%s - entry_price) / entry_price * 100, 2),
                status      = 'closed'
            WHERE id = %s
            """,
            (exit_date, exit_price, exit_reason, exit_price, exit_price, pos_id),
        )


def evaluate_open_positions():
    """Check all open positions and close any that hit target, SL, or timeout."""
    conn = get_connection()
    try:
        open_pos = _get_open_positions(conn)
        if not open_pos:
            print("  [performance] No open positions to evaluate.")
            conn.close()
            return

        print(f"  [performance] Evaluating {len(open_pos)} open position(s)...")
        symbols = [p["symbol"] for p in open_pos]
        ohlc    = _fetch_today_ohlc(symbols)
        today   = date.today()

        closed_count = 0
        with conn:
            for pos in open_pos:
                sym  = pos["symbol"]
                data = ohlc.get(sym)
                days_held = (today - pos["signal_date"]).days

                # Determine exit
                if data is None:
                    if days_held >= MAX_HOLD_DAYS:
                        # No data + timeout → skip (can't close without price)
                        continue
                    continue

                target   = float(pos["target_price"])
                sl       = float(pos["stop_loss_price"])
                high     = data["high"]
                low      = data["low"]
                close    = data["close"]

                if high >= target:
                    _close_position(conn, pos["id"], target, "target_hit", today)
                    closed_count += 1
                    print(f"    {sym}: TARGET HIT  exit=₹{target}")
                elif low <= sl:
                    _close_position(conn, pos["id"], sl, "stop_loss", today)
                    closed_count += 1
                    print(f"    {sym}: STOP LOSS   exit=₹{sl}")
                elif days_held >= MAX_HOLD_DAYS:
                    _close_position(conn, pos["id"], close, "timeout", today)
                    closed_count += 1
                    print(f"    {sym}: TIMEOUT     exit=₹{close} (day {days_held})")
                else:
                    print(f"    {sym}: still open  (day {days_held})")

        print(f"  [performance] Closed {closed_count} position(s).")
    except Exception as e:
        print(f"  [performance] evaluate error: {e}", file=sys.stderr)
    finally:
        conn.close()


def log_new_signals(signals: list[dict], strategy: str):
    """Insert today's signals as new open positions (skip duplicates)."""
    if not signals:
        return
    conn = get_connection()
    try:
        today = date.today()
        inserted = 0
        with conn:
            with conn.cursor() as cur:
                for s in signals:
                    # Avoid duplicate entries if scanner reruns same day
                    cur.execute(
                        f"""
                        SELECT 1 FROM {SCHEMA}.{TABLE}
                        WHERE signal_date = %s AND symbol = %s AND strategy = %s
                        """,
                        (today, s["symbol"], strategy),
                    )
                    if cur.fetchone():
                        continue

                    cur.execute(
                        f"""
                        INSERT INTO {SCHEMA}.{TABLE}
                            (signal_date, strategy, symbol,
                             entry_price, target_price, stop_loss_price, investment)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            today, strategy, s["symbol"],
                            s["entry_min"], s["target"], s["stop_loss"],
                            INVESTMENT,
                        ),
                    )
                    inserted += 1

        if inserted:
            print(f"  [performance] Logged {inserted} new {strategy} position(s).")
    except Exception as e:
        print(f"  [performance] log error: {e}", file=sys.stderr)
    finally:
        conn.close()
