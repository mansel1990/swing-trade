"""
EMA Pullback Strategy Scanner
Finds stocks in an established uptrend that have pulled back to the 20 EMA and are bouncing.

Why this complements the breakout scanner:
  - Breakout scanner: catches stocks about to make new highs (momentum entry)
  - This scanner:     catches stocks dipping in an uptrend (lower-risk, "buy the dip" entry)

Filters (all must pass):
  1. Uptrend confirmed      — 20 EMA > 50 EMA today
  2. Pullback happened      — price touched within 1.5% of 20 EMA in last 3 days
  3. Bounce confirmed       — today's close is above the 20 EMA
  4. Healthy pullback       — volume on the lowest-close day was below 20-day average
  5. RSI in range           — RSI between 40 and 62
  6. Not overextended       — price within 15% above the 50 EMA
"""

import sys
import pandas as pd
from indicators import calculate_rsi, calculate_volume_ratio, calculate_ema

# ── Strategy parameters ───────────────────────────────────────────────────────
EMA_FAST            = 20
EMA_SLOW            = 50
PULLBACK_LOOKBACK   = 5      # days to look back for EMA touch (extended from 3)
PULLBACK_TOLERANCE  = 0.02   # price within 2% of EMA counts as a touch (extended from 1.5%)
RSI_MIN             = 40
RSI_MAX             = 62
MAX_ABOVE_SLOW_EMA  = 0.15   # price must be within 15% above 50 EMA
TARGET_PCT          = 0.04   # +4% from entry
STOP_PCT            = 0.02   # -2% below 20 EMA (one SL component)
STOP_HARD_PCT       = 0.03   # hard floor: SL never more than -3% below entry (R:R guardrail)
ENTRY_MAX_PCT       = 0.01   # entry zone upper bound = 20 EMA + 1%
EMA_RISING_LOOKBACK = 5      # days to check if 20 EMA is rising
BOUNCE_VOL_RATIO    = 1.2    # bounce day volume for "Strong" signal
VOLUME_AVG_DAYS     = 20
MIN_ROWS_NEEDED     = EMA_SLOW + PULLBACK_LOOKBACK + 10
# ─────────────────────────────────────────────────────────────────────────────


def analyse_ema_pullback(symbol: str, df: pd.DataFrame) -> dict | None:
    try:
        close  = df["Close"].dropna()
        volume = df["Volume"].dropna()

        if len(close) < MIN_ROWS_NEEDED:
            return None

        ema20_series = calculate_ema(close, EMA_FAST)
        ema50_series = calculate_ema(close, EMA_SLOW)

        ema20_now  = float(ema20_series.iloc[-1])
        ema50_now  = float(ema50_series.iloc[-1])
        cmp        = round(float(close.iloc[-1]), 2)

        # ── Filter 1: Uptrend — 20 EMA above 50 EMA ──────────────────────────
        if ema20_now <= ema50_now:
            return None

        # ── Filter 6: Not overextended — price within 15% of 50 EMA ──────────
        if cmp > ema50_now * (1 + MAX_ABOVE_SLOW_EMA):
            return None

        # ── Filter 3: Bounce confirmed — today's close above 20 EMA ──────────
        if cmp <= ema20_now:
            return None

        # ── Filter 2: Pullback happened — price touched 20 EMA in last N days ─
        # Look at the last PULLBACK_LOOKBACK days (excluding today)
        recent_closes = close.iloc[-(PULLBACK_LOOKBACK + 1):-1]
        recent_ema20  = ema20_series.iloc[-(PULLBACK_LOOKBACK + 1):-1]
        touched = any(
            abs(c - e) / e <= PULLBACK_TOLERANCE
            for c, e in zip(recent_closes, recent_ema20)
        )
        if not touched:
            return None

        # ── Filter 4: REMOVED — low-volume pullback (A/B test) ────────────────
        # Still need avg_vol for signal-strength calc below
        avg_vol = float(volume.iloc[-(VOLUME_AVG_DAYS + 1):-1].mean())
        if avg_vol == 0:
            return None

        # ── Filter 5: RSI in range ────────────────────────────────────────────
        rsi = calculate_rsi(close)
        if not (RSI_MIN <= rsi <= RSI_MAX):
            return None

        # ── All filters passed — build signal ─────────────────────────────────
        volume_ratio = calculate_volume_ratio(volume, VOLUME_AVG_DAYS)
        ema20_level  = round(ema20_now, 2)

        # Limit order: set entry AT the 20 EMA level (the actual support being bought).
        # This fixes the prior inversion where entry_min (cmp) > entry_max (ema20+1%).
        entry_min = ema20_level          # limit order at the EMA — the real buy zone
        entry_max = round(ema20_now * (1 + ENTRY_MAX_PCT), 2)
        target    = round(entry_min * (1 + TARGET_PCT), 2)

        # Guard: if CMP has already bounced so far that target is <2% away, skip.
        # e.g. EMA=1047, target=1089, CMP=1085 → only 0.4% upside left — useless.
        if target < cmp * 1.02:
            return None
        # Stop loss = tighter of {2% below 20 EMA, 3% below entry}.
        sl_ema    = ema20_now * (1 - STOP_PCT)
        sl_hard   = entry_min * (1 - STOP_HARD_PCT)
        stop_loss = round(max(sl_ema, sl_hard), 2)

        # Signal strength: EMA rising AND today's volume above average
        ema20_5d_ago = float(ema20_series.iloc[-(EMA_RISING_LOOKBACK + 1)])
        ema_rising   = ema20_now > ema20_5d_ago
        bounce_vol   = float(volume.iloc[-1])
        strong       = ema_rising and (bounce_vol / avg_vol >= BOUNCE_VOL_RATIO)

        return {
            "symbol":          symbol.replace(".NS", ""),
            "company_name":    symbol.replace(".NS", ""),
            "cmp":             cmp,
            "breakout_level":  ema20_level,   # stores 20 EMA support level
            "entry_min":       entry_min,
            "entry_max":       entry_max,
            "target":          target,
            "stop_loss":       stop_loss,
            "volume_ratio":    volume_ratio,
            "rsi":             rsi,
            "signal_strength": "Strong" if strong else "Moderate",
        }

    except Exception as e:
        print(f"  [{symbol}] EMA analysis error: {e}", file=sys.stderr)
        return None
