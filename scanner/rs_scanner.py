"""
Relative Strength Resilience Scanner.

Finds stocks holding up (or even rising) while Nifty 50 is flat or weak.
These leaders typically outperform when the market eventually turns.

Regime filter (checked once in main.py before any stock analysis):
  - Nifty 10-day return < +1%  (sideways OR declining market)
  - Previously required >2% drop; relaxed so strategy fires in choppy markets too

Per-stock filters (all must pass):
  1. Stock is resilient  — stock 10-day return > Nifty 10-day return by >=5 pp
  2. Mansfield RS rising — smoothed (stock/nifty) ratio today > same 5 days ago
  3. Stock above 50 EMA  — own trend intact
  4. Higher low pattern  — 10-day low today > 10-day low from 20 days ago
  5. RSI 45-65           — pulled in but not overbought
"""

import sys
import pandas as pd
from indicators import (
    calculate_rsi,
    calculate_volume_ratio,
    calculate_ema,
    mansfield_rs,
)

# ── Strategy parameters ───────────────────────────────────────────────────────
NIFTY_EMA           = 20
NIFTY_DROP_LOOKBACK = 10
NIFTY_MAX_GAIN      = 0.01       # nifty 10-day return must be < +1% (sideways or down)
RS_OUTPERFORM_PP    = 5.0        # stock outperforms by 5 percentage points
STOCK_EMA           = 50
HIGHER_LOW_LOOKBACK = 20
HL_WINDOW           = 10
RSI_MIN             = 45
RSI_MAX             = 65
TARGET_PCT          = 0.06
STOP_PCT            = 0.03
ENTRY_MAX_PCT       = 0.015
VOLUME_AVG_DAYS     = 20
MANSFIELD_LOOKBACK  = 5
MIN_ROWS_NEEDED     = STOCK_EMA + HIGHER_LOW_LOOKBACK + 10
# ─────────────────────────────────────────────────────────────────────────────


def is_nifty_weak(nifty_df: pd.DataFrame) -> bool:
    """
    Pre-check the Nifty regime once per scanner run.
    Returns True when Nifty is NOT in a strong upswing (flat or declining).
    Threshold: 10-day return < +1%.  Fires in sideways AND bear markets.
    """
    try:
        close = nifty_df["Close"].dropna()
        if len(close) < NIFTY_DROP_LOOKBACK + 2:
            return False
        today        = float(close.iloc[-1])
        ten_days_ago = float(close.iloc[-(NIFTY_DROP_LOOKBACK + 1)])
        ten_day_return = (today - ten_days_ago) / ten_days_ago  # positive = up
        return ten_day_return < NIFTY_MAX_GAIN
    except Exception:
        return False


def analyse_rs_resilience(
    symbol: str,
    df: pd.DataFrame,
    nifty_df: pd.DataFrame,
    live_price: float | None = None,
) -> dict | None:
    try:
        close  = df["Close"].dropna()
        low    = df["Low"].dropna()
        volume = df["Volume"].dropna()
        nclose = nifty_df["Close"].dropna()

        if len(close) < MIN_ROWS_NEEDED or len(nclose) < NIFTY_DROP_LOOKBACK + 10:
            return None

        # In mid-day mode live_price is the current intraday price; otherwise use yesterday's close.
        cmp = round(live_price, 2) if live_price is not None else round(float(close.iloc[-1]), 2)

        # ── Filter 2: stock outperforming Nifty over 10 days ─────────────────
        stock_10d_ago = float(close.iloc[-(NIFTY_DROP_LOOKBACK + 1)])
        nifty_10d_ago = float(nclose.iloc[-(NIFTY_DROP_LOOKBACK + 1)])
        nifty_now     = float(nclose.iloc[-1])

        stock_ret = (cmp - stock_10d_ago) / stock_10d_ago * 100
        nifty_ret = (nifty_now - nifty_10d_ago) / nifty_10d_ago * 100
        if (stock_ret - nifty_ret) < RS_OUTPERFORM_PP:
            return None

        # ── Filter 3: Mansfield RS rising ────────────────────────────────────
        rs_now    = mansfield_rs(close, nclose, period=NIFTY_EMA)
        rs_5d_ago = mansfield_rs(
            close.iloc[:-MANSFIELD_LOOKBACK],
            nclose.iloc[:-MANSFIELD_LOOKBACK],
            period=NIFTY_EMA,
        )
        if rs_now <= rs_5d_ago:
            return None

        # ── Filter 4: stock above own 50 EMA ─────────────────────────────────
        ema50_series = calculate_ema(close, STOCK_EMA)
        ema50 = float(ema50_series.iloc[-1])
        if cmp <= ema50:
            return None

        # ── Filter 5: higher low pattern ─────────────────────────────────────
        low_now      = float(low.iloc[-HL_WINDOW:].min())
        low_20d_back = float(low.iloc[-(HIGHER_LOW_LOOKBACK + HL_WINDOW):-HIGHER_LOW_LOOKBACK].min())
        if low_now <= low_20d_back:
            return None

        # ── Filter 6: RSI ────────────────────────────────────────────────────
        rsi = calculate_rsi(close)
        if not (RSI_MIN <= rsi <= RSI_MAX):
            return None

        # ── All filters passed — build signal ────────────────────────────────
        volume_ratio = calculate_volume_ratio(volume, VOLUME_AVG_DAYS)
        ema20_series = calculate_ema(close, NIFTY_EMA)
        ema20_level  = round(float(ema20_series.iloc[-1]), 2)

        # Limit order: set entry 1% below close — these leaders hold their bid
        # so a small pullback at open gets a better fill than chasing at CMP.
        entry_min = round(cmp * 0.99, 2)
        entry_max = cmp                  # don't chase above yesterday's close
        target    = round(entry_min * (1 + TARGET_PCT), 2)
        # Stop is the larger (= tighter, higher) of: 20 EMA -3%, recent swing low
        stop_ema     = ema20_level * (1 - STOP_PCT)
        stop_swing   = float(low.iloc[-HL_WINDOW:].min())
        stop_loss    = round(max(stop_ema, stop_swing), 2)

        # ── Guard: stop must be below entry (can break on tight-range stocks) ──
        # entry_min is CMP−1%; stop_swing is the 10-day low which can sit
        # above entry on stocks with very tight consolidation ranges.
        # A stop above entry produces a positive P&L on a "stop loss" hit —
        # which is nonsensical. Skip the signal instead of emitting bad data.
        if stop_loss >= entry_min:
            return None

        outperformance = stock_ret - nifty_ret
        strong = outperformance >= 10.0 and volume_ratio >= 1.2

        return {
            "symbol":          symbol.replace(".NS", ""),
            "company_name":    symbol.replace(".NS", ""),
            "cmp":             cmp,
            "breakout_level":  ema20_level,    # stores 20 EMA support level
            "entry_min":       entry_min,
            "entry_max":       entry_max,
            "target":          target,
            "stop_loss":       stop_loss,
            "volume_ratio":    volume_ratio,
            "rsi":             rsi,
            "signal_strength": "Strong" if strong else "Moderate",
        }

    except Exception as e:
        print(f"  [{symbol}] RS analysis error: {e}", file=sys.stderr)
        return None
