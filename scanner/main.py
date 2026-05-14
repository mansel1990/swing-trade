"""
Momentum Scanner — runs two strategies on the same batch download each evening.

Strategies:
  1. Breakout  → swing.signals       (consolidation breakout near 10-day high)
  2. EMA Pull  → swing.ema_signals   (pullback to 20 EMA in uptrend, bounce entry)

Usage:
  python main.py                   # scan only, print results
  python main.py --save            # scan + save both strategies to DB
  python main.py --strategy ema    # run only EMA strategy
  python main.py --strategy breakout  # run only breakout strategy
"""

import sys
import time
import yfinance as yf
import pandas as pd
from indicators import calculate_rsi, calculate_volume_ratio, calculate_breakout_level, is_consolidating
from ema_scanner import analyse_ema_pullback
from stocks import STOCKS, BATCH_SIZE

# ── Breakout strategy parameters ─────────────────────────────────────────────
LOOKBACK_DAYS       = 60
CONSOLIDATION_DAYS  = 10
VOLUME_AVG_DAYS     = 20
MIN_VOLUME_RATIO    = 1.5
RSI_MIN             = 50
RSI_MAX             = 68
NEAR_BREAKOUT_PCT   = 0.02
TARGET_PCT          = 0.05
STOP_LOSS_PCT       = 0.025
ENTRY_MAX_PCT       = 0.02
TOP_N               = 10
BATCH_DELAY_SEC     = 2
MIN_ROWS_NEEDED     = CONSOLIDATION_DAYS + VOLUME_AVG_DAYS + 5
# ─────────────────────────────────────────────────────────────────────────────


def fetch_batch(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Download OHLCV for a batch of symbols in a single yfinance call."""
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
                df = raw.copy()
            else:
                df = raw[sym].copy() if sym in raw.columns.get_level_values(0) else pd.DataFrame()

            if df.empty or len(df) < MIN_ROWS_NEEDED:
                continue
            result[sym] = df
        except Exception:
            continue
    return result


def analyse_breakout(symbol: str, df: pd.DataFrame) -> dict | None:
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

        if not consolidating:
            return None
        if volume_ratio < MIN_VOLUME_RATIO:
            return None
        if not (RSI_MIN <= rsi <= RSI_MAX):
            return None

        lower_bound = breakout_level * (1 - NEAR_BREAKOUT_PCT)
        if not (lower_bound <= cmp <= breakout_level):
            return None

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
        print(f"  [{symbol}] breakout error: {e}", file=sys.stderr)
        return None


def print_signal(s: dict, strategy: str = "BREAKOUT"):
    label = "20EMA" if strategy == "EMA" else "Brkout"
    print(
        f"{s['symbol']:<14} | "
        f"CMP: Rs{s['cmp']:<8} | "
        f"{label}: Rs{s['breakout_level']:<8} | "
        f"Entry: Rs{s['entry_min']}-{s['entry_max']} | "
        f"Target: Rs{s['target']} | "
        f"SL: Rs{s['stop_loss']} | "
        f"Vol: {s['volume_ratio']}x | "
        f"RSI: {s['rsi']} | "
        f"{s['signal_strength'].upper()}"
    )


def print_section(title: str, results: list[dict], strategy: str):
    print(f"\n{'-' * 100}")
    print(f"  {title}")
    print(f"{'-' * 100}")
    if not results:
        print("  No setups found today.")
    else:
        for s in results:
            print_signal(s, strategy)


def main(save_to_db: bool = False, strategy: str = "all"):
    run_breakout = strategy in ("all", "breakout")
    run_ema      = strategy in ("all", "ema")

    total   = len(STOCKS)
    batches = [STOCKS[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    print(f"Scanning {total} stocks in {len(batches)} batch(es) | "
          f"Strategies: {'Breakout + EMA Pullback' if strategy == 'all' else strategy.upper()}\n")

    breakout_results = []
    ema_results      = []

    for batch_num, batch in enumerate(batches, 1):
        print(f"  [Batch {batch_num}/{len(batches)}] Downloading {len(batch)} tickers...", flush=True)
        data = fetch_batch(batch)
        print(f"  [Batch {batch_num}/{len(batches)}] Got data for {len(data)}/{len(batch)}. Analysing...")

        for sym in batch:
            if sym not in data:
                continue
            df = data[sym]

            b_signal = analyse_breakout(sym, df)  if run_breakout else None
            e_signal = analyse_ema_pullback(sym, df) if run_ema      else None

            tag = []
            if b_signal:
                breakout_results.append(b_signal)
                tag.append("BREAKOUT")
            if e_signal:
                ema_results.append(e_signal)
                tag.append("EMA-PULL")

            if tag:
                print(f"    {sym:<22} {' + '.join(tag)}")

        if batch_num < len(batches):
            time.sleep(BATCH_DELAY_SEC)

    # Sort and trim
    breakout_results.sort(key=lambda x: x["volume_ratio"], reverse=True)
    ema_results.sort(key=lambda x: x["volume_ratio"], reverse=True)
    top_breakout = breakout_results[:TOP_N]
    top_ema      = ema_results[:TOP_N]

    if run_breakout:
        print_section("BREAKOUT SIGNALS  (entry above consolidation high)", top_breakout, "BREAKOUT")
    if run_ema:
        print_section("EMA PULLBACK SIGNALS  (bounce off 20 EMA in uptrend)", top_ema, "EMA")

    if save_to_db:
        from db import ensure_table, delete_today_signals, save_signals

        if run_breakout:
            ensure_table("signals")
            delete_today_signals("signals")
            if top_breakout:
                save_signals(top_breakout, "signals")
            else:
                print("(Nothing to save - no breakout signals today.)")

        if run_ema:
            ensure_table("ema_signals")
            delete_today_signals("ema_signals")
            if top_ema:
                save_signals(top_ema, "ema_signals")
            else:
                print("(Nothing to save - no EMA pullback signals today.)")

    return top_breakout, top_ema


if __name__ == "__main__":
    save     = "--save" in sys.argv
    strat    = "all"
    if "--strategy" in sys.argv:
        idx = sys.argv.index("--strategy")
        if idx + 1 < len(sys.argv):
            strat = sys.argv[idx + 1]
    main(save_to_db=save, strategy=strat)
