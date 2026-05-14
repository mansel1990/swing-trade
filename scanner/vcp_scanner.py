"""
Volatility Contraction Pattern (VCP) Scanner — Mark Minervini style.

Catches stocks that are coiling tightly under a pivot in a Stage-2 uptrend.
Tight progressive contractions (e.g. 20% -> 12% -> 5%) signal supply drying up
ahead of a breakout.

Filters (all must pass):
  1. Stage 2 uptrend     — Price > 50 EMA > 150 EMA > 200 EMA
  2. Position vs 52w     — up >25% from 52-week low, within 25% of 52-week high
  3. Progressive contractions — 2-4 contractions, each tighter than the previous
  4. Volume dry-up       — final contraction's avg vol < 60% of overall window avg
  5. Near pivot          — today's close within 2% of the pivot (last swing high)
  6. RSI 50-70           — momentum healthy but not overbought
"""

import sys
import pandas as pd
from indicators import (
    calculate_rsi,
    calculate_volume_ratio,
    calculate_ema,
    find_contractions,
)

# ── Strategy parameters ───────────────────────────────────────────────────────
EMA_FAST            = 50
EMA_MID             = 150
EMA_SLOW            = 200
WEEK52_DAYS         = 252
PCT_OFF_HIGH_MAX    = 0.25
PCT_OFF_LOW_MIN     = 0.25
CONTRACTION_LOOKBACK = 50
MIN_CONTRACTIONS    = 2
MAX_CONTRACTIONS    = 4
PIVOT_TOLERANCE     = 0.02
RSI_MIN             = 50
RSI_MAX             = 70
VOL_DRY_UP_RATIO    = 0.60
TARGET_PCT          = 0.08
STOP_BUFFER         = 0.93
ENTRY_MAX_PCT       = 0.02
VOLUME_AVG_DAYS     = 20
MIN_ROWS_NEEDED     = EMA_SLOW + 10
# ─────────────────────────────────────────────────────────────────────────────


def analyse_vcp(symbol: str, df: pd.DataFrame) -> dict | None:
    try:
        close  = df["Close"].dropna()
        high   = df["High"].dropna()
        low    = df["Low"].dropna()
        volume = df["Volume"].dropna()

        if len(close) < MIN_ROWS_NEEDED:
            return None

        cmp = round(float(close.iloc[-1]), 2)

        # ── Filter 1: Stage 2 uptrend ────────────────────────────────────────
        ema50  = float(calculate_ema(close, EMA_FAST).iloc[-1])
        ema150 = float(calculate_ema(close, EMA_MID).iloc[-1])
        ema200 = float(calculate_ema(close, EMA_SLOW).iloc[-1])
        if not (cmp > ema50 > ema150 > ema200):
            return None

        # ── Filter 2: position vs 52-week range ──────────────────────────────
        recent = close.iloc[-WEEK52_DAYS:] if len(close) >= WEEK52_DAYS else close
        high_52 = float(recent.max())
        low_52  = float(recent.min())
        if low_52 == 0:
            return None
        pct_off_high = (high_52 - cmp) / high_52
        pct_off_low  = (cmp - low_52) / low_52
        if pct_off_high > PCT_OFF_HIGH_MAX:
            return None
        if pct_off_low < PCT_OFF_LOW_MIN:
            return None

        # ── Filter 3: progressive contractions ───────────────────────────────
        contractions = find_contractions(high, low, lookback=CONTRACTION_LOOKBACK)
        if len(contractions) < MIN_CONTRACTIONS:
            return None
        # Take the most recent up-to-MAX_CONTRACTIONS contractions
        recent_c = contractions[-MAX_CONTRACTIONS:]
        if len(recent_c) < MIN_CONTRACTIONS:
            return None
        # Strictly progressive: each contraction must be tighter than previous
        progressive = all(recent_c[i] < recent_c[i - 1] for i in range(1, len(recent_c)))
        if not progressive:
            return None

        # ── Filter 4: volume dry-up in final contraction ─────────────────────
        # Use the last ~10 days as a proxy for the final contraction window
        final_window = 10
        avg_vol_window = float(volume.iloc[-CONTRACTION_LOOKBACK:].mean())
        avg_vol_final  = float(volume.iloc[-final_window:].mean())
        if avg_vol_window == 0:
            return None
        if avg_vol_final >= avg_vol_window * VOL_DRY_UP_RATIO:
            return None

        # ── Filter 5: near pivot (last swing high in lookback window) ────────
        pivot = float(high.iloc[-CONTRACTION_LOOKBACK:].max())
        if pivot == 0:
            return None
        distance_to_pivot = (pivot - cmp) / pivot
        if distance_to_pivot > PIVOT_TOLERANCE:
            return None

        # ── Filter 6: RSI ────────────────────────────────────────────────────
        rsi = calculate_rsi(close)
        if not (RSI_MIN <= rsi <= RSI_MAX):
            return None

        # ── All filters passed — build signal ────────────────────────────────
        volume_ratio = calculate_volume_ratio(volume, VOLUME_AVG_DAYS)

        # Stop loss = lowest low of final contraction OR pivot * 0.93, whichever tighter
        final_low = float(low.iloc[-final_window:].min())
        stop_pivot = round(pivot * STOP_BUFFER, 2)
        stop_loss  = round(max(final_low, stop_pivot), 2)

        entry_min = round(pivot, 2)
        entry_max = round(pivot * (1 + ENTRY_MAX_PCT), 2)
        target    = round(pivot * (1 + TARGET_PCT), 2)

        strong = len(recent_c) >= 3 and volume_ratio >= 1.3

        return {
            "symbol":          symbol.replace(".NS", ""),
            "company_name":    symbol.replace(".NS", ""),
            "cmp":             cmp,
            "breakout_level":  entry_min,        # stores pivot price
            "entry_min":       entry_min,
            "entry_max":       entry_max,
            "target":          target,
            "stop_loss":       stop_loss,
            "volume_ratio":    volume_ratio,
            "rsi":             rsi,
            "signal_strength": "Strong" if strong else "Moderate",
        }

    except Exception as e:
        print(f"  [{symbol}] VCP analysis error: {e}", file=sys.stderr)
        return None
