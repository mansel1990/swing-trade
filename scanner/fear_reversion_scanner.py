"""
Fear Factor Reversion Scanner.

Plays the "rubber band effect" when market fear peaks: buy blue-chip stocks that
have been panic-sold to extreme oversold levels at major support, targeting a
bounce back to the 20 EMA.

Designed for sideways / correcting markets where breakouts keep failing.
Only activates when India VIX is elevated (fear in the market = fuel for the bounce).
Only trades Nifty 50 / Next 50 large-cap blue chips (high liquidity, real bounce).

Regime filter (checked once in main.py):
  - India VIX > VIX_THRESHOLD (15 by default)

Per-stock filters (all must pass):
  1. Large-cap universe  — symbol must be in LARGE_CAP_SYMBOLS (Nifty 50 + Next 50)
  2. RSI(14) < 35        — oversold (relaxed from 30 — blue chips rarely touch 30)
  3. At major support    — within 3% of 200 EMA OR within 3% of 52-week low
  4. Bullish reversal    — today is a piercing/engulfing candle (panic → absorption)
  5. Volume confirmation — today's volume > 1.0x 20-day average
  6. Trend not destroyed — price > 200 EMA × 0.85 (not a structural collapse)

Exit logic: target = 20 EMA (the mean reversion magnet — do NOT hold for a new high)
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
VIX_THRESHOLD       = 15.0     # India VIX must be above this (elevated fear)
RSI_MAX             = 35       # oversold threshold (blue chips rarely hit 30)
EMA_TARGET          = 20       # exit target = 20 EMA (mean reversion magnet)
EMA_LONG            = 200      # major support reference
SUPPORT_TOLERANCE   = 0.03     # within 3% of 200 EMA or 52-week low
YEAR_LOW_LOOKBACK   = 252      # trading days in a year (52-week low lookback)
TREND_BREAK_BUFFER  = 0.85     # price must be > 200 EMA * 0.85 (not in collapse)
MIN_VOLUME_RATIO    = 1.0      # volume confirmation
ENTRY_MAX_PCT       = 0.01
STOP_PCT            = 0.03     # stop = today's low * (1 - 0.03)
VOLUME_AVG_DAYS     = 20
MIN_ROWS_NEEDED     = EMA_LONG + 10
# ─────────────────────────────────────────────────────────────────────────────

# Nifty 50 + key Nifty Next 50 stocks — Fear Reversion only trades these.
# High liquidity = panic selling is institutional, not retail; bounce is reliable.
LARGE_CAP_SYMBOLS = frozenset({
    # Nifty 50
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK",
    "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "TITAN",
    "SUNPHARMA", "WIPRO", "ULTRACEMCO", "NESTLEIND", "BAJFINANCE",
    "HCLTECH", "POWERGRID", "NTPC", "ONGC", "TATASTEEL",
    "JSWSTEEL", "TECHM", "ADANIENT", "BAJAJFINSV", "COALINDIA",
    "BRITANNIA", "DIVISLAB", "DRREDDY", "EICHERMOT", "GRASIM",
    "HEROMOTOCO", "HINDALCO", "INDUSINDBK", "M&M", "SBILIFE",
    "TATACONSUM", "CIPLA", "APOLLOHOSP", "HDFCLIFE",
    "BPCL", "ADANIPORTS", "BAJAJ-AUTO", "TATAMOTORS", "UPL", "COFORGE",
    # Nifty Next 50
    "DMART", "SIEMENS", "BOSCHLTD", "GODREJCP", "MARICO",
    "NAUKRI", "MUTHOOTFIN", "CHOLAFIN", "ICICIGI", "ICICIPRULI",
    "HDFCAMC", "IRCTC", "TRENT", "HAVELLS", "BANKBARODA",
    "PNB", "CANBK", "UNIONBANK", "GAIL", "IOC",
    "HINDPETRO", "SAIL", "AMBUJACEM", "DLF", "GODREJPROP",
    "TATAPOWER", "TATACHEM", "CONCOR", "RECLTD", "INDUSTOWER",
})


def is_vix_elevated(vix_df: pd.DataFrame) -> bool:
    """
    Pre-check the India VIX regime once per scanner run.
    Returns True when VIX > VIX_THRESHOLD (fear in the market).
    """
    try:
        close = vix_df["Close"].dropna()
        if len(close) < 2:
            return False
        return float(close.iloc[-1]) > VIX_THRESHOLD
    except Exception:
        return False


def analyse_fear_reversion(symbol: str, df: pd.DataFrame) -> dict | None:
    try:
        base_sym = symbol.replace(".NS", "")

        # ── Filter 1: large-cap universe only ────────────────────────────────
        if base_sym not in LARGE_CAP_SYMBOLS:
            return None

        close  = df["Close"].dropna()
        low    = df["Low"].dropna()
        volume = df["Volume"].dropna()

        if len(close) < MIN_ROWS_NEEDED:
            return None

        cmp = round(float(close.iloc[-1]), 2)

        # ── Filter 6: trend not destroyed ────────────────────────────────────
        ema200 = float(calculate_ema(close, EMA_LONG).iloc[-1])
        if ema200 == 0 or cmp < ema200 * TREND_BREAK_BUFFER:
            return None

        # ── Filter 2: RSI oversold ────────────────────────────────────────────
        rsi = calculate_rsi(close)
        if rsi >= RSI_MAX:
            return None

        # ── Filter 3: at major support ────────────────────────────────────────
        today_low  = float(low.iloc[-1])
        near_ema   = abs(today_low - ema200) / ema200 <= SUPPORT_TOLERANCE

        year_window = min(YEAR_LOW_LOOKBACK, len(low))
        year_low    = float(low.iloc[-year_window:].min())
        near_52w    = abs(today_low - year_low) / year_low <= SUPPORT_TOLERANCE if year_low else False

        if not (near_ema or near_52w):
            return None

        # ── Filter 4: bullish reversal candle ─────────────────────────────────
        if not is_bullish_reversal_candle(df):
            return None

        # ── Filter 5: volume on reversal day ─────────────────────────────────
        volume_ratio = calculate_volume_ratio(volume, VOLUME_AVG_DAYS)
        if volume_ratio < MIN_VOLUME_RATIO:
            return None

        # ── All filters passed — build signal ─────────────────────────────────
        ema20 = float(calculate_ema(close, EMA_TARGET).iloc[-1])

        # Support reference: whichever was hit
        if near_ema:
            support_level = round(ema200, 2)
        else:
            support_level = round(year_low, 2)

        entry_min = cmp
        entry_max = round(cmp * (1 + ENTRY_MAX_PCT), 2)
        # Exit at 20 EMA — do NOT hold for a new high in a sideways market
        target    = round(max(ema20, cmp * 1.03), 2)   # at least +3%, ideally to 20 EMA
        stop_loss = round(today_low * (1 - STOP_PCT), 2)

        # Strong: fully engulfing + heavy volume (institutional absorption)
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
            "symbol":          base_sym,
            "company_name":    base_sym,
            "cmp":             cmp,
            "breakout_level":  support_level,   # stores support reference (200 EMA or 52w low)
            "entry_min":       entry_min,
            "entry_max":       entry_max,
            "target":          target,
            "stop_loss":       stop_loss,
            "volume_ratio":    volume_ratio,
            "rsi":             rsi,
            "signal_strength": "Strong" if strong else "Moderate",
        }

    except Exception as e:
        print(f"  [{symbol}] Fear Reversion analysis error: {e}", file=sys.stderr)
        return None
