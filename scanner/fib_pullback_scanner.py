"""
Fib Pullback Strategy Scanner
Implements the swing-trading setup from "The Only Swing Trading Video Beginners
will Ever Need!" by Trade with Pat (https://www.youtube.com/watch?v=JL7HdUKRxfI).

Why this complements the existing scanners:
  - ema_pullback:  buys ANY pullback near the 20 EMA in a 20>50 stacked uptrend
  - fib_pullback:  buys ONLY when the pullback has retraced past the 50% Fib of
                   the last swing leg (Pat's "discount zone") and a big-body red
                   pullback is followed by a green confirmation candle today.

Filters (all must pass):
  1. Uptrend          — today close > 50 EMA
  2. Swing leg found  — recent swing low -> swing high in the last 30 bars
  3. Discount zone    — today close <= 61.8% retracement (was 50% — tightened after backtest)
  4. Pullback count   — >= 3 red bars in the last 5 bars (excluding today)
  5. Pullback size    — at least one of those red bars has a body >= 1.5x avg body
  6. Confirmation     — today is green AND today close > yesterday close
  7. RSI sanity       — RSI(14) between 30 and 60
  8. Not overextended — today close <= 50 EMA * 1.15
  9. Volume floor     — green candle volume >= 0.8x avg (filters no-volume traps)
"""

import sys
import pandas as pd
from indicators import (
    calculate_rsi,
    calculate_volume_ratio,
    calculate_ema,
    find_swing_high_low,
    fib_retracement_pct,
    has_big_red_pullback,
)

# ── Strategy parameters ───────────────────────────────────────────────────────
EMA_TREND           = 50
SWING_LOOKBACK      = 30
SWING_MIN_BARS      = 5
SWING_MIN_LEG_PCT   = 0.03
FIB_GATE            = 0.618      # must retrace past 61.8% — the real "discount zone"
FIB_STRONG          = 0.78       # >=78% retrace earns "Strong" tag
PULLBACK_LOOKBACK   = 5
PULLBACK_BODY_MULT  = 1.5
PULLBACK_AVG_BASE   = 20
RSI_MIN             = 30
RSI_MAX             = 60
MAX_ABOVE_EMA       = 0.15
ENTRY_MAX_PCT       = 0.005      # entry zone upper bound = close + 0.5%
STOP_BELOW_SUPPORT  = 0.01       # SL 1% below swing low
STOP_HARD_PCT       = 0.03       # hard floor: SL never more than 3% below entry
TARGET_RR           = 3.0        # 1:3 R:R (Pat's framework — keep the wide target, improve win rate via tighter filters)
MIN_VOLUME_RATIO    = 0.8        # green candle needs at least 0.8x avg (filters no-volume traps)
VOLUME_AVG_DAYS     = 20
BOUNCE_VOL_RATIO    = 1.2
MIN_ROWS_NEEDED     = EMA_TREND + SWING_LOOKBACK + PULLBACK_AVG_BASE + 10
# ─────────────────────────────────────────────────────────────────────────────


def analyse_fib_pullback(symbol: str, df: pd.DataFrame) -> dict | None:
    try:
        if len(df) < MIN_ROWS_NEEDED:
            return None

        close  = df["Close"].dropna()
        open_  = df["Open"].dropna()
        high   = df["High"].dropna()
        low    = df["Low"].dropna()
        volume = df["Volume"].dropna()

        if min(len(close), len(open_), len(high), len(low), len(volume)) < MIN_ROWS_NEEDED:
            return None

        # ── Filter 1: Uptrend — close > 50 EMA ───────────────────────────────
        ema50 = float(calculate_ema(close, EMA_TREND).iloc[-1])
        cmp_  = round(float(close.iloc[-1]), 2)
        if cmp_ <= ema50:
            return None

        # ── Filter 8: Not overextended (cheaper than swing detection) ────────
        if cmp_ > ema50 * (1 + MAX_ABOVE_EMA):
            return None

        # ── Filter 6: Today is the green confirmation candle ─────────────────
        today_o   = float(open_.iloc[-1])
        today_c   = float(close.iloc[-1])
        prev_c    = float(close.iloc[-2])
        if not (today_c > today_o and today_c > prev_c):
            return None

        # ── Filter 2: Find the most recent valid swing leg ───────────────────
        swing = find_swing_high_low(
            high, low,
            lookback=SWING_LOOKBACK,
            min_bars_between=SWING_MIN_BARS,
            min_leg_pct=SWING_MIN_LEG_PCT,
        )
        if swing is None:
            return None
        _, _, swing_low, swing_high = swing

        # ── Filter 3: Discount zone — today's close past the 50% retracement ─
        retrace = fib_retracement_pct(swing_low, swing_high, today_c)
        if retrace < FIB_GATE:
            return None

        # ── Filters 4 & 5: Pullback structure (>=3 reds + big body) ──────────
        if not has_big_red_pullback(
            open_, close,
            lookback=PULLBACK_LOOKBACK,
            body_multiplier=PULLBACK_BODY_MULT,
            avg_baseline=PULLBACK_AVG_BASE,
        ):
            return None

        # ── Filter 7: RSI in range ───────────────────────────────────────────
        rsi = calculate_rsi(close)
        if not (RSI_MIN <= rsi <= RSI_MAX):
            return None

        # ── Build signal ─────────────────────────────────────────────────────
        entry_min = cmp_
        entry_max = round(cmp_ * (1 + ENTRY_MAX_PCT), 2)

        # Stop: a little below the swing-low "support" per Pat, capped at 3% below entry.
        sl_support = swing_low * (1 - STOP_BELOW_SUPPORT)
        sl_hard    = entry_min * (1 - STOP_HARD_PCT)
        stop_loss  = round(max(sl_support, sl_hard), 2)

        # Target: 1:3 R:R from entry.
        risk   = entry_min - stop_loss
        if risk <= 0:
            return None
        target = round(entry_min + TARGET_RR * risk, 2)

        volume_ratio = calculate_volume_ratio(volume, VOLUME_AVG_DAYS)

        # ── Volume floor: green candle needs real participation ───────────────
        if volume_ratio < MIN_VOLUME_RATIO:
            return None

        strong = retrace >= FIB_STRONG and volume_ratio >= BOUNCE_VOL_RATIO

        return {
            "symbol":          symbol.replace(".NS", ""),
            "company_name":    symbol.replace(".NS", ""),
            "cmp":             cmp_,
            "breakout_level":  round(swing_low, 2),   # stores swing-low support
            "entry_min":       entry_min,
            "entry_max":       entry_max,
            "target":          target,
            "stop_loss":       stop_loss,
            "volume_ratio":    volume_ratio,
            "rsi":             rsi,
            "signal_strength": "Strong" if strong else "Moderate",
        }

    except Exception as e:
        print(f"  [{symbol}] Fib pullback analysis error: {e}", file=sys.stderr)
        return None
