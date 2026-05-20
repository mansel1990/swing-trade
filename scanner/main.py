"""
Momentum Scanner — runs six strategies on the same batch download each evening.

Strategies:
  1. Breakout       → swing.signals                  (consolidation breakout)
  2. EMA Pull       → swing.ema_signals              (pullback to 20 EMA in uptrend)
  3. VCP            → swing.vcp_signals              (volatility contraction pattern)
  4. RS Resilience  → swing.rs_signals               (outperforming flat/weak Nifty)
  5. Mean Reversion → swing.mean_reversion_signals   (RSI<35 bounce at support)
  6. Fear Reversion → swing.fear_reversion_signals   (VIX spike + large-cap oversold)

Usage:
  python main.py                           # scan only, print results
  python main.py --save                    # scan + save all strategies
  python main.py --strategy ema            # run only EMA strategy
  python main.py --strategy breakout       # run only breakout strategy
  python main.py --strategy vcp            # run only VCP
  python main.py --strategy rs             # run only Relative Strength
  python main.py --strategy mr             # run only Mean Reversion
  python main.py --strategy fr             # run only Fear Reversion
"""

import sys
import time
import yfinance as yf
import pandas as pd
from indicators import calculate_rsi, calculate_volume_ratio, calculate_breakout_level, is_consolidating
from ema_scanner import analyse_ema_pullback
from vcp_scanner import analyse_vcp
from rs_scanner import analyse_rs_resilience, is_nifty_weak
from mean_reversion_scanner import analyse_mean_reversion
from fear_reversion_scanner import analyse_fear_reversion, is_vix_elevated
from stocks import STOCKS, BATCH_SIZE

# ── Breakout strategy parameters ─────────────────────────────────────────────
LOOKBACK_DAYS       = 220   # bumped from 60 — VCP & 200 EMA need long history
CONSOLIDATION_DAYS  = 10
VOLUME_AVG_DAYS     = 20
MIN_VOLUME_RATIO    = 1.5
RSI_MIN             = 50
RSI_MAX             = 68
NEAR_BREAKOUT_PCT   = 0.02
TARGET_PCT          = 0.05
STOP_LOSS_PCT       = 0.025
ENTRY_MAX_PCT       = 0.02
TOP_N               = 5
BATCH_DELAY_SEC     = 2
MIN_ROWS_NEEDED     = CONSOLIDATION_DAYS + VOLUME_AVG_DAYS + 5
NIFTY_TICKER        = "^NSEI"
VIX_TICKER          = "^INDIAVIX"
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


def fetch_index(ticker: str) -> pd.DataFrame:
    """Fetch a single index (e.g. ^NSEI for Nifty 50) for the same lookback."""
    try:
        raw = yf.download(
            ticker,
            period=f"{LOOKBACK_DAYS}d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=False,
        )
        if raw.empty:
            return pd.DataFrame()
        # yfinance may return MultiIndex columns even for single ticker
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        return raw
    except Exception as e:
        print(f"  [index {ticker}] fetch error: {e}", file=sys.stderr)
        return pd.DataFrame()


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
    label_map = {
        "EMA":      "20EMA",
        "VCP":      "Pivot",
        "RS":       "20EMA",
        "MR":       "Suprt",
        "FR":       "Suprt",
        "BREAKOUT": "Brkout",
    }
    label = label_map.get(strategy, "Level")
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


# Strategy registry: (key, label, table, log_strategy_name, runner)
# The runner returns (results_list, signal_label_for_print).
STRATEGY_KEYS = ("breakout", "ema", "vcp", "rs", "mr", "fr")


def main(save_to_db: bool = False, strategy: str = "all"):
    selected = STRATEGY_KEYS if strategy == "all" else (strategy,)

    run_breakout = "breakout" in selected
    run_ema      = "ema"      in selected
    run_vcp      = "vcp"      in selected
    run_rs       = "rs"       in selected
    run_mr       = "mr"       in selected
    run_fr       = "fr"       in selected

    total   = len(STOCKS)
    batches = [STOCKS[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]

    label_map = {
        "all":      "Breakout + EMA + VCP + RS + Mean Reversion + Fear Reversion",
        "breakout": "Breakout",
        "ema":      "EMA Pullback",
        "vcp":      "VCP",
        "rs":       "Relative Strength Resilience",
        "mr":       "Mean Reversion",
        "fr":       "Fear Reversion",
    }
    print(f"Scanning {total} stocks in {len(batches)} batch(es) | "
          f"Strategies: {label_map.get(strategy, strategy)}\n")

    # Fetch Nifty once if RS strategy is active
    nifty_df = pd.DataFrame()
    nifty_weak = False
    if run_rs:
        print("  [Nifty] Downloading ^NSEI for relative strength...", flush=True)
        nifty_df = fetch_index(NIFTY_TICKER)
        if nifty_df.empty:
            print("  [Nifty] WARNING: failed to fetch — RS strategy will be skipped.")
            run_rs = False
        else:
            nifty_weak = is_nifty_weak(nifty_df)
            print(f"  [Nifty] regime: {'flat/weak — RS scan ACTIVE' if nifty_weak else 'strong (+1%+) — RS will skip'}")

    # Fetch India VIX once if Fear Reversion strategy is active
    vix_df = pd.DataFrame()
    vix_high = False
    if run_fr:
        print("  [VIX] Downloading ^INDIAVIX for fear reversion...", flush=True)
        vix_df = fetch_index(VIX_TICKER)
        if vix_df.empty:
            print("  [VIX] WARNING: failed to fetch — Fear Reversion strategy will be skipped.")
            run_fr = False
        else:
            vix_high = is_vix_elevated(vix_df)
            vix_level = round(float(vix_df["Close"].dropna().iloc[-1]), 1)
            print(f"  [VIX] level: {vix_level} — {'ELEVATED (FR scan active)' if vix_high else 'calm (<15) — FR will skip'}")

    breakout_results = []
    ema_results      = []
    vcp_results      = []
    rs_results       = []
    mr_results       = []
    fr_results       = []

    for batch_num, batch in enumerate(batches, 1):
        print(f"  [Batch {batch_num}/{len(batches)}] Downloading {len(batch)} tickers...", flush=True)
        data = fetch_batch(batch)
        print(f"  [Batch {batch_num}/{len(batches)}] Got data for {len(data)}/{len(batch)}. Analysing...")

        for sym in batch:
            if sym not in data:
                continue
            df = data[sym]

            b_signal = analyse_breakout(sym, df)        if run_breakout else None
            e_signal = analyse_ema_pullback(sym, df)    if run_ema      else None
            v_signal = analyse_vcp(sym, df)             if run_vcp      else None
            r_signal = analyse_rs_resilience(sym, df, nifty_df) if (run_rs and nifty_weak) else None
            m_signal = analyse_mean_reversion(sym, df)  if run_mr       else None
            f_signal = analyse_fear_reversion(sym, df)  if (run_fr and vix_high)  else None

            tag = []
            if b_signal:
                breakout_results.append(b_signal); tag.append("BREAKOUT")
            if e_signal:
                ema_results.append(e_signal);      tag.append("EMA-PULL")
            if v_signal:
                vcp_results.append(v_signal);      tag.append("VCP")
            if r_signal:
                rs_results.append(r_signal);       tag.append("RS")
            if m_signal:
                mr_results.append(m_signal);       tag.append("MEAN-REV")
            if f_signal:
                fr_results.append(f_signal);       tag.append("FEAR-REV")

            if tag:
                print(f"    {sym:<22} {' + '.join(tag)}")

        if batch_num < len(batches):
            time.sleep(BATCH_DELAY_SEC)

    # Sort and trim
    for r in (breakout_results, ema_results, vcp_results, rs_results, mr_results, fr_results):
        r.sort(key=lambda x: x["volume_ratio"], reverse=True)
    top_breakout = breakout_results[:TOP_N]
    top_ema      = ema_results[:TOP_N]
    top_vcp      = vcp_results[:TOP_N]
    top_rs       = rs_results[:TOP_N]
    top_mr       = mr_results[:TOP_N]
    top_fr       = fr_results[:TOP_N]

    if run_breakout:
        print_section("BREAKOUT SIGNALS  (entry above consolidation high)", top_breakout, "BREAKOUT")
    if run_ema:
        print_section("EMA PULLBACK SIGNALS  (bounce off 20 EMA in uptrend)", top_ema, "EMA")
    if run_vcp:
        print_section("VCP SIGNALS  (Minervini-style tight contractions near pivot)", top_vcp, "VCP")
    if run_rs:
        print_section("RS RESILIENCE SIGNALS  (outperforming flat/weak Nifty)", top_rs, "RS")
    if run_mr:
        print_section("MEAN REVERSION SIGNALS  (RSI<35 oversold bounce at support)", top_mr, "MR")
    if run_fr:
        print_section("FEAR REVERSION SIGNALS  (VIX spike + large-cap panic reversal)", top_fr, "FR")

    if save_to_db:
        from db import ensure_table, delete_today_signals, save_signals
        from performance import evaluate_open_positions, log_new_signals

        # Step 1: Evaluate existing open positions with today's prices
        print("\n[Performance tracker]")
        evaluate_open_positions()

        save_jobs = [
            (run_breakout, "signals",                  top_breakout, "breakout"),
            (run_ema,      "ema_signals",              top_ema,      "ema_pullback"),
            (run_vcp,      "vcp_signals",              top_vcp,      "vcp"),
            (run_rs,       "rs_signals",               top_rs,       "rs_resilience"),
            (run_mr,       "mean_reversion_signals",   top_mr,       "mean_reversion"),
            (run_fr,       "fear_reversion_signals",   top_fr,       "fear_reversion"),
        ]
        for run_flag, table, top_list, log_name in save_jobs:
            if not run_flag:
                continue
            ensure_table(table)
            delete_today_signals(table)
            if top_list:
                save_signals(top_list, table)
                log_new_signals(top_list, log_name)
            else:
                print(f"(Nothing to save - no {log_name} signals today.)")

    return top_breakout, top_ema, top_vcp, top_rs, top_mr, top_fr


if __name__ == "__main__":
    save     = "--save" in sys.argv
    strat    = "all"
    if "--strategy" in sys.argv:
        idx = sys.argv.index("--strategy")
        if idx + 1 < len(sys.argv):
            strat = sys.argv[idx + 1]
    main(save_to_db=save, strategy=strat)
