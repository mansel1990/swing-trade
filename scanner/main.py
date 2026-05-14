"""
Momentum Breakout Scanner
Runs each evening, scans ~150 Nifty 200 stocks, saves top setups to Neon Postgres.

Strategy filters:
  - Price within 2% BELOW the 10-day breakout level (coiling up)
  - Volume ratio > 1.5x (today vs 20-day average)
  - RSI between 50 and 68 (building momentum, not overbought)
  - 10-day consolidation range < 5%

yfinance efficiency:
  - Stocks are fetched in batches of 50 → only 3 HTTP calls for 150 stocks
  - No per-stock requests; one bulk download per batch
"""

import sys
import time
import yfinance as yf
import pandas as pd
from indicators import calculate_rsi, calculate_volume_ratio, calculate_breakout_level, is_consolidating
from stocks import STOCKS, BATCH_SIZE

# ── Strategy parameters ───────────────────────────────────────────────────────
LOOKBACK_DAYS       = 60      # history window to fetch
CONSOLIDATION_DAYS  = 10      # days for consolidation / breakout level
VOLUME_AVG_DAYS     = 20      # days for average volume baseline
MIN_VOLUME_RATIO    = 1.5
RSI_MIN             = 50
RSI_MAX             = 68
NEAR_BREAKOUT_PCT   = 0.02    # price must be within 2% below breakout level
TARGET_PCT          = 0.05    # +5% from entry
STOP_LOSS_PCT       = 0.025   # -2.5% from entry
ENTRY_MAX_PCT       = 0.02    # entry zone upper bound = breakout + 2%
TOP_N               = 10
BATCH_DELAY_SEC     = 2       # polite pause between batch downloads
MIN_ROWS_NEEDED     = CONSOLIDATION_DAYS + VOLUME_AVG_DAYS + 5
# ─────────────────────────────────────────────────────────────────────────────


def fetch_batch(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Download OHLCV for a batch of symbols in a single yfinance call.
    Returns a dict of {symbol: DataFrame}."""
    tickers_str = " ".join(symbols)
    raw = yf.download(
        tickers_str,
        period=f"{LOOKBACK_DAYS}d",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    result = {}
    for sym in symbols:
        try:
            if len(symbols) == 1:
                # Single ticker: yfinance returns flat columns
                df = raw.copy()
            else:
                df = raw[sym].copy() if sym in raw.columns.get_level_values(0) else pd.DataFrame()

            if df.empty or len(df) < MIN_ROWS_NEEDED:
                continue
            result[sym] = df
        except Exception:
            continue
    return result


def analyse_stock(symbol: str, df: pd.DataFrame) -> dict | None:
    try:
        close  = df["Close"].dropna()
        high   = df["High"].dropna()
        low    = df["Low"].dropna()
        volume = df["Volume"].dropna()

        if len(close) < MIN_ROWS_NEEDED:
            return None

        cmp            = round(float(close.iloc[-1]), 2)
        breakout_level = calculate_breakout_level(high, CONSOLIDATION_DAYS)
        volume_ratio   = calculate_volume_ratio(volume, VOLUME_AVG_DAYS)
        rsi            = calculate_rsi(close)
        consolidating  = is_consolidating(high, low, CONSOLIDATION_DAYS)

        # ── Filters ───────────────────────────────────────────────────────────
        if not consolidating:
            return None
        if volume_ratio < MIN_VOLUME_RATIO:
            return None
        if not (RSI_MIN <= rsi <= RSI_MAX):
            return None

        # Price must be within 2% below the breakout level (coiling just under)
        lower_bound = breakout_level * (1 - NEAR_BREAKOUT_PCT)
        if not (lower_bound <= cmp <= breakout_level):
            return None
        # ─────────────────────────────────────────────────────────────────────

        entry_min = breakout_level
        entry_max = round(breakout_level * (1 + ENTRY_MAX_PCT), 2)
        target    = round(entry_min * (1 + TARGET_PCT), 2)
        stop_loss = round(entry_min * (1 - STOP_LOSS_PCT), 2)

        return {
            "symbol":          symbol.replace(".NS", ""),
            "company_name":    symbol.replace(".NS", ""),
            "cmp":             cmp,
            "breakout_level":  breakout_level,
            "entry_min":       entry_min,
            "entry_max":       entry_max,
            "target":          target,
            "stop_loss":       stop_loss,
            "volume_ratio":    volume_ratio,
            "rsi":             rsi,
            "signal_strength": "Strong" if volume_ratio >= 2.0 else "Moderate",
        }

    except Exception as e:
        print(f"  [{symbol}] analysis error: {e}", file=sys.stderr)
        return None


def print_signal(s: dict):
    print(
        f"{s['symbol']:<14} | "
        f"CMP: Rs{s['cmp']:<8} | "
        f"Breakout: Rs{s['breakout_level']:<8} | "
        f"Entry: Rs{s['entry_min']}-{s['entry_max']} | "
        f"Target: Rs{s['target']} | "
        f"SL: Rs{s['stop_loss']} | "
        f"Vol: {s['volume_ratio']}x | "
        f"RSI: {s['rsi']} | "
        f"{s['signal_strength'].upper()}"
    )


def main(save_to_db: bool = False):
    total = len(STOCKS)
    batches = [STOCKS[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    print(f"Scanning {total} stocks in {len(batches)} batch(es) of up to {BATCH_SIZE}...\n")

    results = []

    for batch_num, batch in enumerate(batches, 1):
        print(f"  [Batch {batch_num}/{len(batches)}] Downloading {len(batch)} tickers...", flush=True)
        data = fetch_batch(batch)
        print(f"  [Batch {batch_num}/{len(batches)}] Got data for {len(data)}/{len(batch)} tickers. Analysing...")

        for sym in batch:
            if sym not in data:
                print(f"    {sym:<22} no data")
                continue
            signal = analyse_stock(sym, data[sym])
            if signal:
                print(f"    {sym:<22} MATCH  -> Vol {signal['volume_ratio']}x | RSI {signal['rsi']}")
                results.append(signal)
            else:
                print(f"    {sym:<22} skip")

        if batch_num < len(batches):
            time.sleep(BATCH_DELAY_SEC)

    # Sort by volume ratio, keep top N
    results.sort(key=lambda x: x["volume_ratio"], reverse=True)
    top = results[:TOP_N]

    print(f"\n{'-' * 100}")
    if not top:
        print("No setups found today.")
    else:
        print(f"\nTop {len(top)} setup(s):\n")
        for s in top:
            print_signal(s)

    if save_to_db and top:
        from db import ensure_table, delete_today_signals, save_signals
        ensure_table()
        delete_today_signals()
        save_signals(top)
    elif save_to_db and not top:
        print("\n(Nothing to save - no signals matched today.)")

    return top


if __name__ == "__main__":
    save = "--save" in sys.argv
    main(save_to_db=save)
