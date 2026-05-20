"""
Mean Reversion Extreme Oversold Scanner.

Buys stocks in long-term uptrends that have been pushed deeply oversold and
show a clear bullish reversal candle at major support. Mean target = 20 EMA.

Filters (all must pass):
  1. RSI(14) < 30        — extreme oversold
  2. At major support    — within 3% of 200 EMA OR within 3% of 60-day low
  3. Bullish reversal    — today is a piercing/engulfing candle on yesterday
  4. Volume confirmation — today's volume > 1.0x 20-day average
  5. Trend not broken    — price > 200 EMA * 0.92
"""

import sys
import pandas as pd
from indicators import (
    calculate_rsi,
    calculate_volume_ratio,
    calculate_ema,
    is_bullish_reversal_candle,
)

# ── Strategy parameters ───────────────────────────────────────────────────────
RSI_MAX             = 35    # relaxed from 30 — catches the 32-35 zone where real reversals start
EMA_TARGET          = 20
EMA_LONG            = 200
SUPPORT_TOLERANCE   = 0.03
SWING_LOW_LOOKBACK  = 60
TREND_BREAK_BUFFER  = 0.92
MIN_VOLUME_RATIO    = 1.0
ENTRY_MAX_PCT       = 0.01
STOP_PCT            = 0.02
VOLUME_AVG_DAYS     = 20
MIN_ROWS_NEEDED     = EMA_LONG + 10
# ─────────────────────────────────────────────────────────────────────────────


def analyse_mean_reversion(symbol: str, df: pd.DataFrame) -> dict | None:
    try:
        close  = df["Close"].dropna()
        low    = df["Low"].dropna()
        volume = df["Volume"].dropna()

        if len(close) < MIN_ROWS_NEEDED:
            return None

        cmp = round(float(close.iloc[-1]), 2)

        # ── Filter 1: RSI < 30 ───────────────────────────────────────────────
        rsi = calculate_rsi(close)
        if rsi >= RSI_MAX:
            return None

        # ── Filter 5: trend not broken (price > 200 EMA * 0.92) ──────────────
        ema200 = float(calculate_ema(close, EMA_LONG).iloc[-1])
        if ema200 == 0 or cmp < ema200 * TREND_BREAK_BUFFER:
            return None

        # ── Filter 2: at major support (200 EMA OR 60-day low) ───────────────
        today_low = float(low.iloc[-1])
        near_ema  = abs(today_low - ema200) / ema200 <= SUPPORT_TOLERANCE
        swing_low = float(low.iloc[-SWING_LOW_LOOKBACK:].min())
        near_swing = abs(today_low - swing_low) / swing_low <= SUPPORT_TOLERANCE if swing_low else False
        if not (near_ema or near_swing):
            return None

        # ── Filter 3: bullish reversal candle ────────────────────────────────
        if not is_bullish_reversal_candle(df):
            return None

        # ── Filter 4: volume on reversal day > 1.0x avg ──────────────────────
        volume_ratio = calculate_volume_ratio(volume, VOLUME_AVG_DAYS)
        if volume_ratio < MIN_VOLUME_RATIO:
            return None

        # ── All filters passed — build signal ────────────────────────────────
        ema20 = float(calculate_ema(close, EMA_TARGET).iloc[-1])
        # Reference support level: whichever was hit
        if near_ema:
            support_level = round(ema200, 2)
        else:
            support_level = round(swing_low, 2)

        entry_min = cmp
        entry_max = round(cmp * (1 + ENTRY_MAX_PCT), 2)
        target    = round(max(ema20, cmp * 1.04), 2)  # at least +4%, ideally to 20 EMA
        stop_loss = round(today_low * (1 - STOP_PCT), 2)

        # Strong if today fully engulfs yesterday + heavy volume
        try:
            today_o = float(df["Open"].iloc[-1])
            today_c = float(df["Close"].iloc[-1])
            prev_o  = float(df["Open"].iloc[-2])
            prev_c  = float(df["Close"].iloc[-2])
            engulfs = today_c >= prev_o and today_o <= prev_c
        except Exception:
            engulfs = False
        strong = engulfs and volume_ratio >= 1.5

        return {
            "symbol":          symbol.replace(".NS", ""),
            "company_name":    symbol.replace(".NS", ""),
            "cmp":             cmp,
            "breakout_level":  support_level,    # stores support reference
            "entry_min":       entry_min,
            "entry_max":       entry_max,
            "target":          target,
            "stop_loss":       stop_loss,
            "volume_ratio":    volume_ratio,
            "rsi":             rsi,
            "signal_strength": "Strong" if strong else "Moderate",
        }

    except Exception as e:
        print(f"  [{symbol}] Mean Reversion analysis error: {e}", file=sys.stderr)
        return None
