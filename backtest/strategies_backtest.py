"""
All-Strategy Backtest + Coordinate-Descent Optimiser

Covers:
  1. breakout      — consolidation breakout on volume
  2. ema_pullback  — bounce off 20 EMA in a 20>50 uptrend
  3. vcp           — Minervini volatility contraction near pivot
  4. fib_pullback  — 50% Fib retrace + green confirmation candle
  5. rs_resilience — stock outperforming a weak Nifty (needs nifty CSV)
  6. mean_reversion — RSI<30 bounce at 200 EMA / swing-low support
     (reuses mean_reversion_backtest.py's engine — not duplicated here)

Data source : daily_data/*_daily.csv  (no DB, no internet)
Nifty data  : daily_data/NIFTY50_daily.csv  (optional; RS strategy skipped if absent)

Usage:
  python strategies_backtest.py
  python strategies_backtest.py --strategy ema_pullback
  python strategies_backtest.py --strategy all --days 180 --top-n 10
  python strategies_backtest.py --no-optimise   # skip coord-descent
"""

from __future__ import annotations

import argparse
import csv
import itertools
import os
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, timedelta
from glob import glob
from typing import Any

import numpy as np
import pandas as pd

# ── path bootstrap ────────────────────────────────────────────────────────────
_HERE        = os.path.dirname(os.path.abspath(__file__))
_SCANNER_DIR = os.path.join(os.path.dirname(_HERE), "scanner")
if _SCANNER_DIR not in sys.path:
    sys.path.insert(0, _SCANNER_DIR)

from indicators import find_contractions, find_swing_high_low

INVESTMENT    = 10_000
MAX_HOLD_DAYS = 15
NATURAL_EXITS = {"target_hit", "stop_loss", "timeout", "eod_sample"}

NIFTY_FILE_NAMES = [
    "NIFTY50_daily.csv", "NIFTY_50_daily.csv", "NIFTY_daily.csv",
    "^NSEI_daily.csv", "NSEI_daily.csv",
]

# ═════════════════════════════════════════════════════════════════════════════
#  STRATEGY PARAMETER DEFAULTS + GRIDS
# ═════════════════════════════════════════════════════════════════════════════

DEFAULTS: dict[str, dict[str, Any]] = {

    "breakout": dict(
        consolidation_days=10,
        volume_avg_days=20,
        min_volume_ratio=1.5,
        rsi_min=50,
        rsi_max=68,
        near_breakout_pct=0.02,
        target_pct=0.05,
        stop_loss_pct=0.025,
        entry_max_pct=0.02,
    ),

    "ema_pullback": dict(
        ema_fast=20,
        ema_slow=50,
        pullback_lookback=3,
        pullback_tolerance=0.015,
        rsi_min=40,
        rsi_max=62,
        max_above_slow_ema=0.15,
        target_pct=0.04,
        stop_pct=0.02,
        stop_hard_pct=0.03,
        entry_max_pct=0.01,
        volume_avg_days=20,
    ),

    "vcp": dict(
        ema_fast=50,
        ema_mid=150,
        ema_slow=200,
        pct_off_high_max=0.25,
        pct_off_low_min=0.25,
        contraction_lookback=50,
        min_contractions=2,
        max_contractions=4,
        pivot_tolerance=0.02,
        rsi_min=50,
        rsi_max=70,
        vol_dry_up_ratio=0.60,
        target_pct=0.08,
        stop_buffer=0.93,
        entry_max_pct=0.02,
        volume_avg_days=20,
    ),

    "fib_pullback": dict(
        ema_trend=50,
        swing_lookback=30,
        swing_min_bars=5,
        swing_min_leg_pct=0.03,
        fib_gate=0.5,
        pullback_lookback=5,
        pullback_body_mult=1.5,
        pullback_avg_base=20,
        rsi_min=30,
        rsi_max=60,
        max_above_ema=0.15,
        entry_max_pct=0.005,
        stop_below_support=0.01,
        stop_hard_pct=0.03,
        target_rr=3.0,
        volume_avg_days=20,
    ),

    "rs_resilience": dict(
        nifty_ema=20,
        nifty_drop_lookback=10,
        nifty_min_drop=0.02,
        rs_outperform_pp=5.0,
        stock_ema=50,
        higher_low_lookback=20,
        hl_window=10,
        rsi_min=45,
        rsi_max=65,
        target_pct=0.06,
        stop_pct=0.03,
        entry_max_pct=0.015,
        volume_avg_days=20,
        mansfield_lookback=5,
    ),
}

GRIDS: dict[str, dict[str, list]] = {

    "breakout": {
        "min_volume_ratio": [1.2, 1.5, 2.0],
        "rsi_min":          [45, 50, 55],
        "rsi_max":          [65, 68, 72],
        "near_breakout_pct":[0.01, 0.02, 0.03],
        "target_pct":       [0.04, 0.05, 0.07],
        "stop_loss_pct":    [0.02, 0.025, 0.03],
    },

    "ema_pullback": {
        "pullback_tolerance":[0.01, 0.015, 0.02],
        "rsi_min":           [35, 40, 45],
        "rsi_max":           [58, 62, 66],
        "target_pct":        [0.03, 0.04, 0.06],
        "stop_pct":          [0.015, 0.02, 0.025],
        "max_above_slow_ema":[0.10, 0.15, 0.20],
    },

    "vcp": {
        "pivot_tolerance":   [0.01, 0.02, 0.03],
        "rsi_min":           [45, 50, 55],
        "rsi_max":           [68, 70, 75],
        "target_pct":        [0.06, 0.08, 0.10],
        "vol_dry_up_ratio":  [0.50, 0.60, 0.70],
        "stop_buffer":       [0.91, 0.93, 0.95],
    },

    "fib_pullback": {
        "fib_gate":          [0.382, 0.5, 0.618],
        "rsi_min":           [25, 30, 35],
        "rsi_max":           [55, 60, 65],
        "target_rr":         [2.0, 3.0, 4.0],
        "stop_hard_pct":     [0.02, 0.03, 0.04],
        "swing_lookback":    [20, 30, 40],
    },

    "rs_resilience": {
        "rs_outperform_pp":  [3.0, 5.0, 7.0],
        "nifty_min_drop":    [0.01, 0.02, 0.03],
        "rsi_min":           [40, 45, 50],
        "rsi_max":           [60, 65, 70],
        "target_pct":        [0.04, 0.06, 0.08],
        "stop_pct":          [0.02, 0.03, 0.04],
    },
}

# ═════════════════════════════════════════════════════════════════════════════
#  FAST NUMPY INDICATOR HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1)
    out   = np.empty(len(arr), dtype=np.float64)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def _rsi_series(close: np.ndarray, period: int = 14) -> np.ndarray:
    n     = len(close)
    out   = np.full(n, np.nan)
    if n < 2:
        return out
    alpha  = 1.0 / period
    delta  = np.diff(close)
    gain   = np.where(delta > 0, delta, 0.0)
    loss   = np.where(delta < 0, -delta, 0.0)
    ag, al = gain[0], loss[0]
    rs     = ag / al if al != 0 else np.inf
    out[1] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(1, n - 1):
        ag = alpha * gain[i] + (1.0 - alpha) * ag
        al = alpha * loss[i] + (1.0 - alpha) * al
        rs = ag / al if al != 0 else np.inf
        out[i + 1] = 100.0 - 100.0 / (1.0 + rs)
    return out


def _vol_ratio(volume: np.ndarray, avg_period: int, i: int) -> float:
    if i < avg_period:
        return 0.0
    avg = volume[i - avg_period : i].mean()
    return 0.0 if avg == 0 else round(float(volume[i] / avg), 2)


def _is_consolidating(high: np.ndarray, low: np.ndarray, i: int, days: int, thr: float = 0.05) -> bool:
    if i < days:
        return False
    wh = high[i - days : i]
    wl = low[i - days : i]
    if wl.min() == 0:
        return False
    return (wh.max() - wl.min()) / wl.min() < thr


def _breakout_level(high: np.ndarray, i: int, lookback: int) -> float:
    if i < lookback:
        return 0.0
    return float(high[i - lookback : i].max())


def _mansfield_rs_now_and_past(
    stock_close: np.ndarray,
    nifty_close: np.ndarray,
    period: int,
    lookback: int,
) -> tuple[float, float]:
    """Returns (rs_now, rs_N_days_ago) using aligned arrays (same length)."""
    if len(stock_close) < period + lookback + 1:
        return 0.0, 0.0
    ratio = stock_close / np.where(nifty_close != 0, nifty_close, np.nan)
    # EWM smoothing
    alpha = 2.0 / (period + 1)
    sm    = np.empty(len(ratio))
    sm[0] = ratio[0] if not np.isnan(ratio[0]) else 0.0
    for i in range(1, len(ratio)):
        v      = ratio[i] if not np.isnan(ratio[i]) else sm[i - 1]
        sm[i]  = alpha * v + (1.0 - alpha) * sm[i - 1]
    return float(sm[-1]), float(sm[-1 - lookback])


# ═════════════════════════════════════════════════════════════════════════════
#  PER-STRATEGY SIGNAL DETECTORS  (index-based, no DataFrame slicing)
# ═════════════════════════════════════════════════════════════════════════════

def _sig_breakout(
    ticker: str,
    open_: np.ndarray, high: np.ndarray, low: np.ndarray,
    close: np.ndarray, volume: np.ndarray,
    rsi_arr: np.ndarray,
    i: int, p: dict,
) -> dict | None:
    min_rows = p["consolidation_days"] + p["volume_avg_days"] + 5
    if i + 1 < min_rows:
        return None

    cmp  = close[i]
    rsi  = rsi_arr[i]
    if np.isnan(rsi):
        return None

    vr  = _vol_ratio(volume, p["volume_avg_days"], i)
    if vr < p["min_volume_ratio"]:
        return None

    if not (p["rsi_min"] <= rsi <= p["rsi_max"]):
        return None

    if not _is_consolidating(high, low, i, p["consolidation_days"]):
        return None

    bl = _breakout_level(high, i, p["consolidation_days"])
    if bl == 0:
        return None

    lower = bl * (1 - p["near_breakout_pct"])
    if not (lower <= cmp <= bl):
        return None

    entry_min = bl
    entry_max = round(bl * (1 + p["entry_max_pct"]), 2)
    target    = round(entry_min * (1 + p["target_pct"]), 2)
    stop_loss = round(entry_min * (1 - p["stop_loss_pct"]), 2)
    return _sig(ticker, cmp, entry_min, entry_max, target, stop_loss, vr, rsi)


def _sig_ema_pullback(
    ticker: str,
    open_: np.ndarray, high: np.ndarray, low: np.ndarray,
    close: np.ndarray, volume: np.ndarray,
    rsi_arr: np.ndarray,
    ema20_arr: np.ndarray, ema50_arr: np.ndarray,
    i: int, p: dict,
) -> dict | None:
    min_rows = p["ema_slow"] + p["pullback_lookback"] + 10
    if i + 1 < min_rows:
        return None

    cmp    = close[i]
    rsi    = rsi_arr[i]
    ema20  = ema20_arr[i]
    ema50  = ema50_arr[i]

    if np.isnan(rsi):
        return None
    if ema20 <= ema50:
        return None
    if cmp > ema50 * (1 + p["max_above_slow_ema"]):
        return None
    if cmp <= ema20:
        return None
    if not (p["rsi_min"] <= rsi <= p["rsi_max"]):
        return None

    # Pullback touch: any of last N days (excl today) within tolerance of its EMA20
    lb = p["pullback_lookback"]
    touched = False
    for j in range(i - lb, i):
        if j < 0:
            continue
        if abs(close[j] - ema20_arr[j]) / ema20_arr[j] <= p["pullback_tolerance"]:
            touched = True
            break
    if not touched:
        return None

    vr = _vol_ratio(volume, p["volume_avg_days"], i)

    entry_min = cmp
    entry_max = round(ema20 * (1 + p["entry_max_pct"]), 2)
    target    = round(entry_min * (1 + p["target_pct"]), 2)
    sl_ema    = ema20 * (1 - p["stop_pct"])
    sl_hard   = entry_min * (1 - p["stop_hard_pct"])
    stop_loss = round(max(sl_ema, sl_hard), 2)
    return _sig(ticker, cmp, entry_min, entry_max, target, stop_loss, vr, rsi)


def _sig_vcp(
    ticker: str,
    open_: np.ndarray, high: np.ndarray, low: np.ndarray,
    close: np.ndarray, volume: np.ndarray,
    rsi_arr: np.ndarray,
    ema50_arr: np.ndarray, ema150_arr: np.ndarray, ema200_arr: np.ndarray,
    i: int, p: dict,
    high_series: pd.Series, low_series: pd.Series,
) -> dict | None:
    min_rows = p["ema_slow"] + 10
    if i + 1 < min_rows:
        return None

    cmp    = close[i]
    rsi    = rsi_arr[i]
    ema50  = ema50_arr[i]
    ema150 = ema150_arr[i]
    ema200 = ema200_arr[i]

    if np.isnan(rsi):
        return None
    if not (cmp > ema50 > ema150 > ema200):
        return None
    if not (p["rsi_min"] <= rsi <= p["rsi_max"]):
        return None

    # 52-week range
    w52 = min(252, i + 1)
    recent_close = close[i + 1 - w52 : i + 1]
    high_52  = recent_close.max()
    low_52   = recent_close.min()
    if low_52 == 0:
        return None
    if (high_52 - cmp) / high_52 > p["pct_off_high_max"]:
        return None
    if (cmp - low_52) / low_52 < p["pct_off_low_min"]:
        return None

    # Progressive contractions (reuse pandas-based helper with sliced series)
    lb = p["contraction_lookback"]
    h_sl = high_series.iloc[max(0, i + 1 - lb) : i + 1]
    l_sl = low_series.iloc[max(0, i + 1 - lb) : i + 1]
    contractions = find_contractions(h_sl, l_sl, lookback=lb)
    if len(contractions) < p["min_contractions"]:
        return None
    recent_c = contractions[-p["max_contractions"]:]
    if len(recent_c) < p["min_contractions"]:
        return None
    if not all(recent_c[j] < recent_c[j - 1] for j in range(1, len(recent_c))):
        return None

    # Volume dry-up
    final_win = 10
    if i + 1 < lb + final_win:
        return None
    avg_vol_win   = float(volume[i + 1 - lb : i + 1].mean())
    avg_vol_final = float(volume[i + 1 - final_win : i + 1].mean())
    if avg_vol_win == 0 or avg_vol_final >= avg_vol_win * p["vol_dry_up_ratio"]:
        return None

    # Near pivot
    pivot = float(high[i + 1 - lb : i + 1].max())
    if pivot == 0 or (pivot - cmp) / pivot > p["pivot_tolerance"]:
        return None

    vr        = _vol_ratio(volume, p["volume_avg_days"], i)
    final_low = float(low[i + 1 - final_win : i + 1].min())
    stop_loss = round(max(final_low, pivot * p["stop_buffer"]), 2)
    entry_min = round(pivot, 2)
    entry_max = round(pivot * (1 + p["entry_max_pct"]), 2)
    target    = round(pivot * (1 + p["target_pct"]), 2)
    return _sig(ticker, cmp, entry_min, entry_max, target, stop_loss, vr, rsi)


def _sig_fib(
    ticker: str,
    open_: np.ndarray, high: np.ndarray, low: np.ndarray,
    close: np.ndarray, volume: np.ndarray,
    rsi_arr: np.ndarray,
    ema50_arr: np.ndarray,
    i: int, p: dict,
    high_series: pd.Series, low_series: pd.Series,
    open_series: pd.Series, close_series: pd.Series,
) -> dict | None:
    min_rows = p["ema_trend"] + p["swing_lookback"] + p["pullback_avg_base"] + 10
    if i + 1 < min_rows:
        return None

    cmp   = close[i]
    rsi   = rsi_arr[i]
    ema50 = ema50_arr[i]

    if np.isnan(rsi):
        return None
    if cmp <= ema50 or cmp > ema50 * (1 + p["max_above_ema"]):
        return None

    # Green confirmation candle today
    if not (close[i] > open_[i] and close[i] > close[i - 1]):
        return None

    if not (p["rsi_min"] <= rsi <= p["rsi_max"]):
        return None

    # Swing high/low via pandas helper (sliced)
    lb = p["swing_lookback"]
    h_sl  = high_series.iloc[max(0, i + 1 - lb - 1) : i + 1]
    l_sl  = low_series.iloc[max(0, i + 1 - lb - 1) : i + 1]
    swing = find_swing_high_low(
        h_sl, l_sl,
        lookback=lb,
        min_bars_between=p["swing_min_bars"],
        min_leg_pct=p["swing_min_leg_pct"],
    )
    if swing is None:
        return None
    _, _, swing_low, swing_high = swing

    # Fib gate
    if swing_high <= swing_low:
        return None
    retrace = (swing_high - cmp) / (swing_high - swing_low)
    if retrace < p["fib_gate"]:
        return None

    # Big red pullback
    pb_lb   = p["pullback_lookback"]
    pb_base = p["pullback_avg_base"]
    o_sl = open_series.iloc[max(0, i - pb_lb - pb_base) : i + 1]
    c_sl = close_series.iloc[max(0, i - pb_lb - pb_base) : i + 1]
    from indicators import has_big_red_pullback
    if not has_big_red_pullback(o_sl, c_sl,
                                lookback=pb_lb,
                                body_multiplier=p["pullback_body_mult"],
                                avg_baseline=pb_base):
        return None

    vr        = _vol_ratio(volume, p["volume_avg_days"], i)
    entry_min = cmp
    entry_max = round(cmp * (1 + p["entry_max_pct"]), 2)
    sl_sup    = swing_low * (1 - p["stop_below_support"])
    sl_hard   = entry_min * (1 - p["stop_hard_pct"])
    stop_loss = round(max(sl_sup, sl_hard), 2)
    risk      = entry_min - stop_loss
    if risk <= 0:
        return None
    target = round(entry_min + p["target_rr"] * risk, 2)
    return _sig(ticker, cmp, entry_min, entry_max, target, stop_loss, vr, rsi)


def _sig_rs(
    ticker: str,
    open_: np.ndarray, high: np.ndarray, low: np.ndarray,
    close: np.ndarray, volume: np.ndarray,
    rsi_arr: np.ndarray,
    ema50_arr: np.ndarray, ema20_arr: np.ndarray,
    nifty_close: np.ndarray,
    i: int, p: dict,
) -> dict | None:
    min_rows = p["stock_ema"] + p["higher_low_lookback"] + 10
    if i + 1 < min_rows:
        return None
    if i >= len(nifty_close) or i < p["nifty_drop_lookback"]:
        return None

    cmp   = close[i]
    rsi   = rsi_arr[i]
    ema50 = ema50_arr[i]
    ema20 = ema20_arr[i]

    if np.isnan(rsi):
        return None

    # Nifty weak check (at bar i)
    nifty_now    = nifty_close[i]
    nifty_ema_v  = _ema(nifty_close[: i + 1], p["nifty_ema"])[-1]
    if nifty_now >= nifty_ema_v:
        return None
    n10ago = nifty_close[i - p["nifty_drop_lookback"]]
    if n10ago == 0 or (n10ago - nifty_now) / n10ago < p["nifty_min_drop"]:
        return None

    # Stock outperformance
    s10ago = close[i - p["nifty_drop_lookback"]]
    if s10ago == 0:
        return None
    stock_ret = (cmp - s10ago) / s10ago * 100
    nifty_ret = (nifty_now - n10ago) / n10ago * 100
    if (stock_ret - nifty_ret) < p["rs_outperform_pp"]:
        return None

    # Mansfield RS rising
    ml = p["mansfield_lookback"]
    rs_now, rs_past = _mansfield_rs_now_and_past(
        close[: i + 1], nifty_close[: i + 1], p["nifty_ema"], ml,
    )
    if rs_now <= rs_past:
        return None

    if cmp <= ema50:
        return None
    if not (p["rsi_min"] <= rsi <= p["rsi_max"]):
        return None

    # Higher low pattern
    hl_lb  = p["higher_low_lookback"]
    hl_win = p["hl_window"]
    if i < hl_lb + hl_win:
        return None
    low_now     = float(low[i - hl_win + 1 : i + 1].min())
    low_20back  = float(low[i - hl_lb - hl_win + 1 : i - hl_lb + 1].min())
    if low_now <= low_20back:
        return None

    vr        = _vol_ratio(volume, p["volume_avg_days"], i)
    entry_min = cmp
    entry_max = round(cmp * (1 + p["entry_max_pct"]), 2)
    target    = round(entry_min * (1 + p["target_pct"]), 2)
    sl_ema    = ema20 * (1 - p["stop_pct"])
    sl_swing  = low_now
    stop_loss = round(max(sl_ema, sl_swing), 2)
    return _sig(ticker, cmp, entry_min, entry_max, target, stop_loss, vr, rsi)


def _sig(
    ticker: str, cmp: float,
    entry_min: float, entry_max: float,
    target: float, stop_loss: float,
    volume_ratio: float, rsi: float,
) -> dict:
    return {
        "symbol":       ticker,
        "cmp":          round(cmp, 2),
        "entry_min":    entry_min,
        "entry_max":    entry_max,
        "target":       target,
        "stop_loss":    stop_loss,
        "volume_ratio": volume_ratio,
        "rsi":          round(rsi, 2),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  TICKER CACHE  (pre-computed arrays shared across all strategies)
# ═════════════════════════════════════════════════════════════════════════════

class TickerCache:
    __slots__ = (
        "ticker", "dates", "open_", "high", "low", "close", "volume",
        "n", "high_series", "low_series", "open_series", "close_series",
        # indicators keyed by period
        "_ema_cache", "_rsi14",
    )

    def __init__(self, ticker: str, df: pd.DataFrame):
        self.ticker = ticker
        self.dates  = df.index.values
        self.open_  = df["Open"].values.astype(np.float64)
        self.high   = df["High"].values.astype(np.float64)
        self.low    = df["Low"].values.astype(np.float64)
        self.close  = df["Close"].values.astype(np.float64)
        self.volume = df["Volume"].values.astype(np.float64)
        self.n      = len(self.close)
        # Keep pandas Series for helpers that need them (find_contractions etc.)
        self.high_series  = df["High"]
        self.low_series   = df["Low"]
        self.open_series  = df["Open"]
        self.close_series = df["Close"]
        self._ema_cache: dict[int, np.ndarray] = {}
        self._rsi14 = _rsi_series(self.close, 14)

    def ema(self, period: int) -> np.ndarray:
        if period not in self._ema_cache:
            self._ema_cache[period] = _ema(self.close, period)
        return self._ema_cache[period]

    def rsi14(self) -> np.ndarray:
        return self._rsi14


# ═════════════════════════════════════════════════════════════════════════════
#  EXIT SIMULATION  (vectorised numpy)
# ═════════════════════════════════════════════════════════════════════════════

def _simulate_exit(
    cache: TickerCache,
    fill_bar: int,
    fill_price: float,
    target: float,
    stop_loss: float,
    signal_ord: int,
) -> dict:
    start = fill_bar + 1
    if start >= cache.n:
        return _no_fwd()

    high  = cache.high[start:]
    low   = cache.low[start:]
    close = cache.close[start:]
    dates = cache.dates[start:]

    held = np.array(
        [(pd.Timestamp(d).date().toordinal() - signal_ord) for d in dates],
        dtype=np.int32,
    )

    t_arr = high >= target
    s_arr = low  <= stop_loss
    x_arr = held >= MAX_HOLD_DAYS

    inf = len(dates)
    t_i = int(np.argmax(t_arr)) if t_arr.any() else inf
    s_i = int(np.argmax(s_arr)) if s_arr.any() else inf
    x_i = int(np.argmax(x_arr)) if x_arr.any() else inf

    if not t_arr.any(): t_i = inf
    if not s_arr.any(): s_i = inf
    if not x_arr.any(): x_i = inf

    first = min(t_i, s_i, x_i)
    if first == inf:
        d  = pd.Timestamp(dates[-1]).date()
        px = float(close[-1])
        return _exit_r(d, px, "eod_sample", fill_price, int(held[-1]))

    d    = pd.Timestamp(dates[first]).date()
    held_v = int(held[first])
    if first == t_i:
        return _exit_r(d, target,    "target_hit", fill_price, held_v)
    if first == s_i:
        return _exit_r(d, stop_loss, "stop_loss",  fill_price, held_v)
    return _exit_r(d, float(close[first]), "timeout", fill_price, held_v)


def _exit_r(exit_date, exit_price, reason, fill_price, held):
    pnl_pct = round((exit_price - fill_price) / fill_price * 100, 4)
    return dict(exit_date=exit_date, exit_price=exit_price, exit_reason=reason,
                pnl_pct=pnl_pct, holding_days=held)


def _no_fwd():
    return dict(exit_date=None, exit_price=None, exit_reason="no_forward_data",
                pnl_pct=None, holding_days=None)


# ═════════════════════════════════════════════════════════════════════════════
#  DATA LOADING
# ═════════════════════════════════════════════════════════════════════════════

def load_daily_csvs(data_dir: str) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """
    Returns (stock_data, nifty_df).
    nifty_df is empty if no Nifty CSV is found.
    """
    pattern = os.path.join(data_dir, "*_daily.csv")
    files   = sorted(glob(pattern))
    if not files:
        sys.exit(f"No *_daily.csv files found in: {data_dir}")

    required = {"Date", "Open", "High", "Low", "Close", "Volume"}
    result: dict[str, pd.DataFrame] = {}
    nifty_df = pd.DataFrame()
    skipped  = 0

    # Identify nifty file
    nifty_basenames = {n.lower() for n in NIFTY_FILE_NAMES}

    for fpath in files:
        basename = os.path.basename(fpath)
        ticker   = basename.replace("_daily.csv", "")
        try:
            df = pd.read_csv(fpath, parse_dates=["Date"], low_memory=False)
            df.columns = [c.strip() for c in df.columns]
            if not required.issubset(set(df.columns)):
                skipped += 1
                continue
            df = df.sort_values("Date").drop_duplicates("Date")
            df.set_index("Date", inplace=True)
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)
            if len(df) < 50:
                skipped += 1
                continue

            if basename.lower() in nifty_basenames:
                nifty_df = df[["Open", "High", "Low", "Close", "Volume"]]
            else:
                result[ticker] = df[["Open", "High", "Low", "Close", "Volume"]]
        except Exception:
            skipped += 1

    print(f"Loaded {len(result)} tickers  ({skipped} skipped).")
    if nifty_df.empty:
        print("  No Nifty CSV found — rs_resilience will be skipped.")
    else:
        print(f"  Nifty data: {len(nifty_df)} rows.")
    return result, nifty_df


def build_caches(all_data: dict[str, pd.DataFrame]) -> dict[str, TickerCache]:
    return {t: TickerCache(t, df) for t, df in all_data.items()}


def _align_nifty(nifty_df: pd.DataFrame, stock_df: pd.DataFrame) -> np.ndarray:
    """Reindex nifty to stock dates (forward-fill), return close array."""
    if nifty_df.empty:
        return np.array([])
    aligned = nifty_df["Close"].reindex(stock_df.index, method="ffill").fillna(0)
    return aligned.values.astype(np.float64)


# ═════════════════════════════════════════════════════════════════════════════
#  CORE BACKTEST ENGINE
# ═════════════════════════════════════════════════════════════════════════════

MIN_HISTORY = 210


def run_backtest(
    strategy: str,
    caches: dict[str, TickerCache],
    all_data: dict[str, pd.DataFrame],
    nifty_df: pd.DataFrame,
    start_date: date,
    end_date: date,
    params: dict,
) -> list[dict]:
    start_ts = pd.Timestamp(start_date)
    end_ts   = pd.Timestamp(end_date)

    # Build trading-day union
    combined: pd.DatetimeIndex = pd.DatetimeIndex([])
    for df in all_data.values():
        combined = combined.union(df.index)
    trading_days = combined[(combined >= start_ts) & (combined <= end_ts)]

    # Pre-align Nifty close to each ticker's index (only needed for RS)
    nifty_aligned: dict[str, np.ndarray] = {}
    if strategy == "rs_resilience" and not nifty_df.empty:
        for ticker, df in all_data.items():
            nifty_aligned[ticker] = _align_nifty(nifty_df, df)

    trades: list[dict] = []

    for D in trading_days:
        Dd   = D.date()
        Dord = Dd.toordinal()

        for ticker, cache in caches.items():
            i = int(np.searchsorted(cache.dates, D.value, side="right")) - 1
            if i < 0 or i + 1 < MIN_HISTORY:
                continue

            sig = _detect(strategy, ticker, cache, i, params,
                          nifty_aligned.get(ticker, np.array([])))
            if sig is None:
                continue

            fill_bar = i + 1
            if fill_bar >= cache.n:
                trades.append(_trade_row(Dd, ticker, strategy, sig, None, None, "no_forward_data", None))
                continue

            next_open = cache.open_[fill_bar]
            next_high = cache.high[fill_bar]
            emin = sig["entry_min"]
            emax = sig["entry_max"]

            if next_open > emax:
                trades.append(_trade_row(Dd, ticker, strategy, sig, None, None, "stale_entry", None))
                continue
            if next_high < emin:
                trades.append(_trade_row(Dd, ticker, strategy, sig, None, None, "not_filled", None))
                continue

            fill = float(min(max(next_open, emin), emax))
            ex   = _simulate_exit(cache, fill_bar, fill, sig["target"], sig["stop_loss"], Dord)
            trades.append(_trade_row(Dd, ticker, strategy, sig, fill,
                                     ex["exit_date"], ex["exit_reason"],
                                     ex["pnl_pct"], ex.get("holding_days")))

    return trades


def _detect(
    strategy: str,
    ticker: str,
    cache: TickerCache,
    i: int,
    params: dict,
    nifty_close: np.ndarray,
) -> dict | None:
    rsi  = cache.rsi14()
    o, h, l, c, v = cache.open_, cache.high, cache.low, cache.close, cache.volume

    if strategy == "breakout":
        return _sig_breakout(ticker, o, h, l, c, v, rsi, i, params)

    if strategy == "ema_pullback":
        ema20 = cache.ema(params["ema_fast"])
        ema50 = cache.ema(params["ema_slow"])
        return _sig_ema_pullback(ticker, o, h, l, c, v, rsi, ema20, ema50, i, params)

    if strategy == "vcp":
        ema50  = cache.ema(params["ema_fast"])
        ema150 = cache.ema(params["ema_mid"])
        ema200 = cache.ema(params["ema_slow"])
        return _sig_vcp(ticker, o, h, l, c, v, rsi,
                        ema50, ema150, ema200, i, params,
                        cache.high_series, cache.low_series)

    if strategy == "fib_pullback":
        ema50 = cache.ema(params["ema_trend"])
        return _sig_fib(ticker, o, h, l, c, v, rsi, ema50, i, params,
                        cache.high_series, cache.low_series,
                        cache.open_series, cache.close_series)

    if strategy == "rs_resilience":
        if len(nifty_close) == 0:
            return None
        ema50 = cache.ema(params["stock_ema"])
        ema20 = cache.ema(params["nifty_ema"])
        return _sig_rs(ticker, o, h, l, c, v, rsi, ema50, ema20,
                       nifty_close, i, params)

    return None


def _trade_row(
    signal_date: date, ticker: str, strategy: str, sig: dict,
    fill_price, exit_date, exit_reason: str,
    pnl_pct, holding_days=None,
) -> dict:
    return {
        "signal_date":  signal_date.isoformat(),
        "strategy":     strategy,
        "ticker":       ticker,
        "cmp":          sig["cmp"],
        "entry_min":    sig["entry_min"],
        "entry_max":    sig["entry_max"],
        "target":       sig["target"],
        "stop_loss":    sig["stop_loss"],
        "volume_ratio": sig["volume_ratio"],
        "rsi":          sig["rsi"],
        "fill_price":   round(fill_price, 4) if fill_price is not None else "",
        "exit_date":    exit_date.isoformat() if exit_date else "",
        "exit_reason":  exit_reason,
        "pnl_pct":      pnl_pct if pnl_pct is not None else "",
        "holding_days": holding_days if holding_days is not None else "",
    }


# ═════════════════════════════════════════════════════════════════════════════
#  SUMMARY STATS
# ═════════════════════════════════════════════════════════════════════════════

def compute_stats(trades: list[dict]) -> dict:
    filled     = [t for t in trades if t["exit_reason"] in NATURAL_EXITS]
    pnl_trades = [t for t in filled if t["pnl_pct"] != ""]
    empty = dict(total_signals=len(trades), total_filled=len(filled),
                 total_trades=0, win_trades=0, loss_trades=0,
                 win_rate=0.0, avg_pnl_pct=0.0, avg_holding_days=0.0,
                 target_hit=0, stop_loss=0, timeout=0)
    if not pnl_trades:
        return empty
    pnl_vals  = [float(t["pnl_pct"]) for t in pnl_trades]
    hold_vals = [int(t["holding_days"]) for t in pnl_trades if t["holding_days"] != ""]
    wins      = sum(1 for v in pnl_vals if v > 0)
    reasons   = Counter(t["exit_reason"] for t in pnl_trades)
    return dict(
        total_signals=len(trades),
        total_filled=len(filled),
        total_trades=len(pnl_trades),
        win_trades=wins,
        loss_trades=len(pnl_trades) - wins,
        win_rate=round(100.0 * wins / len(pnl_trades), 2),
        avg_pnl_pct=round(sum(pnl_vals) / len(pnl_vals), 4),
        avg_holding_days=round(sum(hold_vals) / len(hold_vals), 1) if hold_vals else 0.0,
        target_hit=reasons["target_hit"],
        stop_loss=reasons["stop_loss"],
        timeout=reasons["timeout"],
    )


def print_stats(stats: dict, label: str) -> None:
    bar = "=" * 66
    print(f"\n{bar}")
    print(f"  {label}")
    print(bar)
    print(f"  Total signals   : {stats['total_signals']}")
    print(f"  Filled trades   : {stats['total_filled']}")
    print(f"  Completed       : {stats['total_trades']}")
    print(f"  Profitable      : {stats['win_trades']}")
    print(f"  Losing          : {stats['loss_trades']}")
    if stats["total_trades"]:
        print(f"  Win rate        : {stats['win_rate']}%")
        print(f"  Avg PnL %       : {stats['avg_pnl_pct']}%")
        print(f"  Avg hold days   : {stats['avg_holding_days']}")
        print(f"  target={stats['target_hit']}  stop={stats['stop_loss']}  "
              f"timeout={stats['timeout']}")
    print(bar)


# ═════════════════════════════════════════════════════════════════════════════
#  COORDINATE-DESCENT OPTIMISER
# ═════════════════════════════════════════════════════════════════════════════

def _score(
    strategy: str,
    caches: dict[str, TickerCache],
    all_data: dict[str, pd.DataFrame],
    nifty_df: pd.DataFrame,
    start_date: date,
    end_date: date,
    p: dict,
    min_trades: int = 5,
) -> tuple[float, float, dict]:
    try:
        trades = run_backtest(strategy, caches, all_data, nifty_df,
                              start_date, end_date, p)
        st = compute_stats(trades)
    except Exception:
        return -float("inf"), 0.0, {}
    if st["total_trades"] < min_trades:
        return -float("inf"), 0.0, st
    return st["avg_pnl_pct"], st["win_rate"], st


def _coord_descent(
    strategy: str,
    caches: dict[str, TickerCache],
    all_data: dict[str, pd.DataFrame],
    nifty_df: pd.DataFrame,
    start_date: date,
    end_date: date,
    start_params: dict,
    visited: dict,
    label: str = "",
) -> tuple[dict, float, float, dict, int]:
    current   = dict(start_params)
    evals_box = [0]
    grid      = GRIDS[strategy]

    def cached_score(p: dict) -> tuple[float, float, dict]:
        key = tuple(sorted(p.items()))
        if key not in visited:
            pnl, wr, st = _score(strategy, caches, all_data, nifty_df,
                                  start_date, end_date, p)
            visited[key] = (pnl, wr, st)
            evals_box[0] += 1
        return visited[key]

    best_pnl, best_wr, best_st = cached_score(current)

    improved = True
    while improved:
        improved = False
        for axis, candidates in grid.items():
            best_val = current[axis]
            ax_pnl, ax_wr, ax_st = best_pnl, best_wr, best_st
            for val in candidates:
                if val == current[axis]:
                    continue
                probe = dict(current)
                probe[axis] = val
                pnl, wr, st = cached_score(probe)
                if (pnl, wr) > (ax_pnl, ax_wr):
                    ax_pnl, ax_wr, ax_st, best_val = pnl, wr, st, val
            if best_val != current[axis]:
                current[axis] = best_val
                best_pnl, best_wr, best_st = ax_pnl, ax_wr, ax_st
                improved = True

    if label:
        keys = list(grid.keys())
        pstr = "  ".join(f"{k}={current[k]}" for k in keys)
        print(f"  [{strategy}] seed={label:<10}  evals={evals_box[0]:3d}  "
              f"avg_pnl={best_pnl:+.4f}%  win={best_wr:.1f}%  {pstr}")

    return current, best_pnl, best_wr, best_st, evals_box[0]


def _make_seeds(strategy: str, base: dict) -> list[tuple[str, dict]]:
    grid = GRIDS[strategy]
    seeds = [("default", dict(base))]
    low  = dict(base)
    high = dict(base)
    mid  = dict(base)
    for k, vals in grid.items():
        low[k]  = vals[0]
        high[k] = vals[-1]
        mid[k]  = vals[len(vals) // 2]
    seeds += [("all_low", low), ("all_high", high), ("mid", mid)]
    return seeds


def _cd_worker(args: tuple) -> tuple:
    (strategy, all_data, nifty_df, start_date, end_date,
     seed_params, label) = args
    caches  = build_caches(all_data)
    visited: dict = {}
    best_p, best_pnl, best_wr, best_st, evals = _coord_descent(
        strategy, caches, all_data, nifty_df,
        start_date, end_date, seed_params, visited, label,
    )
    return best_p, best_pnl, best_wr, best_st, evals, label


def optimise(
    strategy: str,
    all_data: dict[str, pd.DataFrame],
    nifty_df: pd.DataFrame,
    start_date: date,
    end_date: date,
    top_n: int = 10,
    workers: int | None = None,
) -> list[dict]:
    grid       = GRIDS[strategy]
    exhaustive = len(list(itertools.product(*grid.values())))
    seeds      = _make_seeds(strategy, DEFAULTS[strategy])
    n_seeds    = len(seeds)
    w          = workers or min(n_seeds, os.cpu_count() or 1)
    print(f"\n  [{strategy}] coord-descent  seeds={n_seeds}  workers={w}"
          f"  (vs {exhaustive} exhaustive)")

    job_args = [
        (strategy, all_data, nifty_df, start_date, end_date, sp, lbl)
        for lbl, sp in seeds
    ]

    all_rows: list[dict] = []
    total_evals = 0

    with ProcessPoolExecutor(max_workers=w) as pool:
        futs = {pool.submit(_cd_worker, a): a for a in job_args}
        for fut in as_completed(futs):
            p, pnl, wr, st, evals, label = fut.result()
            total_evals += evals
            if pnl == -float("inf") or not st:
                continue
            row: dict = {"avg_pnl_pct": pnl, "seed": label, "evals": evals}
            for k in grid:
                row[k] = p[k]
            row.update({
                "total_trades":     st.get("total_trades", 0),
                "win_trades":       st.get("win_trades", 0),
                "loss_trades":      st.get("loss_trades", 0),
                "win_rate":         st.get("win_rate", 0.0),
                "avg_holding_days": st.get("avg_holding_days", 0.0),
            })
            all_rows.append(row)

    print(f"  [{strategy}] total evals: {total_evals}  (vs {exhaustive} exhaustive)")

    # De-duplicate by param set
    seen: dict[tuple, dict] = {}
    for r in all_rows:
        key = tuple(r[k] for k in grid)
        if key not in seen or r["avg_pnl_pct"] > seen[key]["avg_pnl_pct"]:
            seen[key] = r
    return sorted(seen.values(), key=lambda r: r["avg_pnl_pct"], reverse=True)[:top_n]


# ═════════════════════════════════════════════════════════════════════════════
#  PRINT & CSV OUTPUT
# ═════════════════════════════════════════════════════════════════════════════

def print_opt_results(strategy: str, results: list[dict]) -> None:
    if not results:
        print(f"  [{strategy}] No valid results (need ≥5 trades per run).")
        return
    grid = GRIDS[strategy]
    keys = list(grid.keys())
    bar  = "=" * (20 + 9 * len(keys) + 44)
    print(f"\n{bar}")
    print(f"  [{strategy.upper()}] OPTIMISER RESULTS  (objective: avg_pnl_pct)")
    print(bar)
    param_hdr = "  ".join(f"{k:>8}" for k in keys)
    print(f"  {'#':>3}  {'avgPnL%':>8}  {param_hdr}  "
          f"{'trades':>6}  {'wins':>5}  {'loss':>5}  {'win%':>6}  "
          f"{'hold':>5}  {'evals':>5}  {'seed'}")
    print("-" * (20 + 9 * len(keys) + 44))
    for rank, r in enumerate(results, 1):
        params_str = "  ".join(f"{r[k]:>8}" for k in keys)
        print(f"  {rank:>3}  {r['avg_pnl_pct']:>8.4f}  {params_str}  "
              f"{r['total_trades']:>6}  {r['win_trades']:>5}  {r['loss_trades']:>5}  "
              f"{r['win_rate']:>6.1f}  {r['avg_holding_days']:>5.1f}  "
              f"{r.get('evals','?'):>5}  {r.get('seed','')}")
    print(bar)
    best = results[0]
    print(f"\n  BEST PARAMETERS for {strategy}:")
    for k in keys:
        print(f"    {k:30s} = {best[k]}")
    print(f"    {'avg_pnl_pct':30s} = {best['avg_pnl_pct']:+.4f}%")
    print(f"    {'win_rate':30s} = {best['win_rate']:.1f}%")
    print(f"    {'total_trades':30s} = {best['total_trades']}")
    print()


def write_outputs(
    out_dir: str,
    strategy: str,
    trades: list[dict],
    opt_results: list[dict],
    label: str,
) -> None:
    from datetime import datetime
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if trades:
        path = os.path.join(out_dir, f"{strategy}_trades_{label}_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader(); w.writerows(trades)
        print(f"  Trades CSV  : {path}")

    if opt_results:
        path = os.path.join(out_dir, f"{strategy}_optim_{label}_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(opt_results[0].keys()))
            w.writeheader(); w.writerows(opt_results)
        print(f"  Optim CSV   : {path}")


# ═════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

ALL_STRATEGIES = ["breakout", "ema_pullback", "vcp", "fib_pullback", "rs_resilience"]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="All-strategy backtest + coord-descent optimiser on daily CSVs."
    )
    ap.add_argument(
        "--data-dir",
        default=os.path.join(os.path.dirname(os.path.dirname(_HERE)), "daily_data"),
    )
    ap.add_argument(
        "--strategy", default="all",
        help=f"One of: all, {', '.join(ALL_STRATEGIES)}",
    )
    ap.add_argument("--days",    type=int, default=180)
    ap.add_argument("--top-n",  type=int, default=10)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--no-optimise", action="store_true")
    ap.add_argument("--out-dir", default=os.path.join(_HERE, "reports"))
    args = ap.parse_args()

    strategies = (
        ALL_STRATEGIES if args.strategy == "all"
        else [args.strategy]
    )
    for s in strategies:
        if s not in ALL_STRATEGIES:
            ap.error(f"Unknown strategy '{s}'. Choose from: all, {', '.join(ALL_STRATEGIES)}")

    end_date   = date.today()
    start_date = end_date - timedelta(days=args.days)
    print(f"Backtest window : {start_date}  →  {end_date}  ({args.days} days)")
    print(f"Strategies      : {', '.join(strategies)}")

    all_data, nifty_df = load_daily_csvs(args.data_dir)
    caches = build_caches(all_data)

    label = f"{args.days}d"

    for strategy in strategies:
        if strategy == "rs_resilience" and nifty_df.empty:
            print(f"\n[{strategy}] SKIPPED — no Nifty CSV in {args.data_dir}")
            continue

        print(f"\n{'='*66}")
        print(f"  STRATEGY: {strategy.upper()}")
        print(f"{'='*66}")

        print(f"Running baseline backtest …")
        trades = run_backtest(
            strategy, caches, all_data, nifty_df,
            start_date, end_date, DEFAULTS[strategy],
        )
        stats = compute_stats(trades)
        print_stats(stats, label=f"{strategy} — last {args.days} days (default params)")

        opt_results: list[dict] = []
        if not args.no_optimise:
            opt_results = optimise(
                strategy, all_data, nifty_df,
                start_date, end_date,
                top_n=args.top_n, workers=args.workers,
            )
            print_opt_results(strategy, opt_results)

        write_outputs(args.out_dir, strategy, trades, opt_results, label)


if __name__ == "__main__":
    main()
