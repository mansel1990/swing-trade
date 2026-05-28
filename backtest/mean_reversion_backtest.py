"""
Mean Reversion Strategy — Standalone Backtest + Grid Search

Loads daily OHLCV CSVs from `daily_data/` (no database needed).
Runs a walk-forward backtest over the last 180 calendar days and then
performs a parallel grid search over strategy parameters.

Usage:
  python mean_reversion_backtest.py
  python mean_reversion_backtest.py --data-dir d:/sanjay_swing/daily_data
  python mean_reversion_backtest.py --days 180 --top-n 10 --no-grid

Outputs:
  • Console summary : total trades, avg PnL%, avg holding days,
                      profitable & losing trade counts
  • Grid search table: best parameter sets ranked by win_rate × avg_pnl_pct
  • CSV files in backtest/reports/: trades detail + grid search results
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

# ── Path bootstrap ────────────────────────────────────────────────────────────
_HERE        = os.path.dirname(os.path.abspath(__file__))
_SCANNER_DIR = os.path.join(os.path.dirname(_HERE), "scanner")
if _SCANNER_DIR not in sys.path:
    sys.path.insert(0, _SCANNER_DIR)

# ─────────────────────────────────────────────────────────────────────────────
#  DEFAULT STRATEGY PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_PARAMS: dict[str, Any] = dict(
    rsi_max=30,
    ema_target=20,
    ema_long=200,
    support_tolerance=0.03,
    swing_low_lookback=60,
    trend_break_buffer=0.92,
    min_volume_ratio=1.0,
    entry_max_pct=0.01,
    stop_pct=0.02,
    volume_avg_days=20,
)

INVESTMENT    = 10_000
MAX_HOLD_DAYS = 15          # calendar days before timeout exit

NATURAL_EXITS = {"target_hit", "stop_loss", "timeout", "eod_sample"}

GRID: dict[str, list] = {
    "rsi_max":            [25, 30, 35],
    "support_tolerance":  [0.02, 0.03, 0.05],
    "trend_break_buffer": [0.88, 0.92, 0.95],
    "min_volume_ratio":   [0.8, 1.0, 1.2],
    "stop_pct":           [0.015, 0.02, 0.03],
}


# ═════════════════════════════════════════════════════════════════════════════
#  FAST INDICATOR HELPERS  (operate directly on numpy arrays)
# ═════════════════════════════════════════════════════════════════════════════

def _ema_array(arr: np.ndarray, period: int) -> np.ndarray:
    """Wilder-style EMA (same as pandas ewm span=period, adjust=False)."""
    alpha = 2.0 / (period + 1)
    out   = np.empty(len(arr), dtype=np.float64)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def _rsi_last(close: np.ndarray, period: int = 14) -> float:
    """RSI of the last bar, computed from the full close array."""
    delta = np.diff(close)
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    # EWM with com = period-1
    alpha = 1.0 / period
    ag, al = gain[0], loss[0]
    for g, l in zip(gain[1:], loss[1:]):
        ag = alpha * g + (1.0 - alpha) * ag
        al = alpha * l + (1.0 - alpha) * al
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def _volume_ratio(volume: np.ndarray, avg_period: int) -> float:
    """Today's volume / avg of prior `avg_period` days."""
    if len(volume) < avg_period + 1:
        return 0.0
    avg = volume[-(avg_period + 1):-1].mean()
    return 0.0 if avg == 0 else round(float(volume[-1] / avg), 2)


def _is_bullish_reversal(open_: np.ndarray, close: np.ndarray) -> bool:
    """
    Piercing / engulfing at the last bar:
      - Today green, yesterday red, today closes higher
      - Today's close >= midpoint of yesterday's body (piercing or stronger)
    """
    if len(open_) < 2:
        return False
    to, tc = open_[-1], close[-1]
    po, pc = open_[-2], close[-2]
    today_green  = tc > to
    prev_red     = pc < po
    higher_close = tc > pc
    if not (today_green and prev_red and higher_close):
        return False
    prev_body = abs(po - pc)
    if prev_body == 0:
        return False
    midpoint = (po + pc) / 2.0
    return tc >= midpoint


# ═════════════════════════════════════════════════════════════════════════════
#  PRE-COMPUTED TICKER CACHE
#  We pre-compute full-history indicator arrays once per ticker so that the
#  inner walk-forward loop only slices numpy arrays (O(1)) instead of
#  recomputing rolling indicators from scratch each day (O(n)).
# ═════════════════════════════════════════════════════════════════════════════

class TickerCache:
    """
    Holds pre-computed indicator series aligned to the ticker's date index.
    All arrays are float64 numpy arrays, same length as dates.
    """
    __slots__ = (
        "dates",      # np.ndarray of np.datetime64
        "open_",      # np.ndarray
        "high",       # np.ndarray
        "low",        # np.ndarray
        "close",      # np.ndarray
        "volume",     # np.ndarray
        "ema200",     # np.ndarray  — precomputed for default ema_long=200
        "ema20",      # np.ndarray  — precomputed for default ema_target=20
        "rsi14",      # np.ndarray  — RSI at each bar
        "n",          # int
    )

    def __init__(self, df: pd.DataFrame, p: dict):
        self.dates  = df.index.values                          # datetime64[ns]
        self.open_  = df["Open"].values.astype(np.float64)
        self.high   = df["High"].values.astype(np.float64)
        self.low    = df["Low"].values.astype(np.float64)
        self.close  = df["Close"].values.astype(np.float64)
        self.volume = df["Volume"].values.astype(np.float64)
        self.n      = len(self.close)

        # Pre-compute EMA series once for the full history
        self.ema200 = _ema_array(self.close, p["ema_long"])
        self.ema20  = _ema_array(self.close, p["ema_target"])

        # Pre-compute RSI at every bar using a forward pass
        self.rsi14  = self._compute_rsi_series(p)

    def _compute_rsi_series(self, p: dict) -> np.ndarray:
        period = 14
        alpha  = 1.0 / period
        close  = self.close
        n      = self.n
        out    = np.full(n, np.nan)
        if n < 2:
            return out
        delta = np.diff(close)
        gain  = np.where(delta > 0, delta, 0.0)
        loss  = np.where(delta < 0, -delta, 0.0)
        ag, al = gain[0], loss[0]
        out[1] = 100.0 - 100.0 / (1.0 + (ag / al if al != 0 else np.inf))
        for i in range(1, n - 1):
            ag = alpha * gain[i] + (1.0 - alpha) * ag
            al = alpha * loss[i] + (1.0 - alpha) * al
            rs = ag / al if al != 0 else np.inf
            out[i + 1] = 100.0 - 100.0 / (1.0 + rs)
        return out


def build_caches(
    all_data: dict[str, pd.DataFrame],
    p: dict,
) -> dict[str, TickerCache]:
    return {ticker: TickerCache(df, p) for ticker, df in all_data.items()}


# ═════════════════════════════════════════════════════════════════════════════
#  SIGNAL DETECTION  (works on a slice index into the cache)
# ═════════════════════════════════════════════════════════════════════════════

def detect_signal(
    ticker: str,
    cache: TickerCache,
    i: int,           # index of signal bar (inclusive)
    p: dict,
) -> dict | None:
    """
    Run all mean-reversion filters at bar index `i`.
    All indicator values come from pre-computed cache arrays — O(1) reads.
    """
    min_rows = p["ema_long"] + 10
    if i + 1 < min_rows:
        return None

    cmp      = cache.close[i]
    rsi      = cache.rsi14[i]
    ema200   = cache.ema200[i]
    ema20    = cache.ema20[i]
    today_low = cache.low[i]

    if np.isnan(rsi) or rsi >= p["rsi_max"]:
        return None
    if ema200 == 0 or cmp < ema200 * p["trend_break_buffer"]:
        return None

    # Support check (200 EMA or swing low)
    near_ema = abs(today_low - ema200) / ema200 <= p["support_tolerance"]
    lookback  = min(p["swing_low_lookback"], i)
    swing_low_val = float(cache.low[i - lookback : i + 1].min()) if lookback > 0 else 0.0
    near_swing = (
        abs(today_low - swing_low_val) / swing_low_val <= p["support_tolerance"]
        if swing_low_val > 0 else False
    )
    if not (near_ema or near_swing):
        return None

    # Bullish reversal candle (needs open of yesterday)
    if i < 1:
        return None
    if not _is_bullish_reversal(cache.open_[i - 1 : i + 1], cache.close[i - 1 : i + 1]):
        return None

    # Volume filter
    vol_ratio = _volume_ratio(cache.volume[: i + 1], p["volume_avg_days"])
    if vol_ratio < p["min_volume_ratio"]:
        return None

    support_level = round(ema200, 2) if near_ema else round(swing_low_val, 2)
    entry_min     = cmp
    entry_max     = round(cmp * (1.0 + p["entry_max_pct"]), 2)
    target        = round(max(ema20, cmp * 1.04), 2)
    stop_loss     = round(today_low * (1.0 - p["stop_pct"]), 2)

    return {
        "symbol":        ticker,
        "cmp":           round(cmp, 2),
        "entry_min":     entry_min,
        "entry_max":     entry_max,
        "target":        target,
        "stop_loss":     stop_loss,
        "volume_ratio":  vol_ratio,
        "rsi":           round(rsi, 2),
        "support_level": support_level,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  FAST EXIT SIMULATION  (vectorised numpy)
# ═════════════════════════════════════════════════════════════════════════════

def simulate_exit_fast(
    cache: TickerCache,
    fill_bar: int,        # index of fill bar (the day after signal)
    fill_price: float,
    target: float,
    stop_loss: float,
    signal_date_ord: int, # date.toordinal() of signal day
) -> dict:
    """
    Vectorised exit: find first bar after fill where target/stop/timeout fires.
    Uses numpy argmax on boolean arrays — much faster than Python loops.
    """
    # Bars available for exit start the day AFTER the fill bar
    start = fill_bar + 1
    if start >= cache.n:
        return _no_forward_result()

    high  = cache.high[start:]
    low   = cache.low[start:]
    close = cache.close[start:]
    dates = cache.dates[start:]

    days_held = np.array(
        [(pd.Timestamp(d).date().toordinal() - signal_date_ord) for d in dates],
        dtype=np.int32,
    )

    target_hit  = high >= target
    stop_hit    = low  <= stop_loss
    timeout_hit = days_held >= MAX_HOLD_DAYS

    # First event index for each exit type
    t_idx = int(np.argmax(target_hit))  if target_hit.any()  else -1
    s_idx = int(np.argmax(stop_hit))    if stop_hit.any()    else -1
    x_idx = int(np.argmax(timeout_hit)) if timeout_hit.any() else -1

    # Whichever fires first wins; -1 means never fired → treat as infinity
    inf = len(dates)
    t_i = t_idx if t_idx >= 0 and target_hit[t_idx]  else inf
    s_i = s_idx if s_idx >= 0 and stop_hit[s_idx]    else inf
    x_i = x_idx if x_idx >= 0 and timeout_hit[x_idx] else inf

    first = min(t_i, s_i, x_i)
    if first == inf:
        # Ran out of data — exit at last available close
        d    = pd.Timestamp(dates[-1]).date()
        px   = float(close[-1])
        held = int(days_held[-1])
        return _exit_result(d, px, "eod_sample", fill_price, held)

    d    = pd.Timestamp(dates[first]).date()
    held = int(days_held[first])

    if first == t_i:
        return _exit_result(d, target,    "target_hit", fill_price, held)
    if first == s_i:
        return _exit_result(d, stop_loss, "stop_loss",  fill_price, held)
    return _exit_result(d, float(close[first]), "timeout", fill_price, held)


def _exit_result(
    exit_date: date, exit_price: float, reason: str,
    fill_price: float, holding_days: int,
) -> dict:
    pnl_pct = round((exit_price - fill_price) / fill_price * 100, 4)
    return dict(exit_date=exit_date, exit_price=exit_price, exit_reason=reason,
                pnl_pct=pnl_pct, holding_days=holding_days)


def _no_forward_result() -> dict:
    return dict(exit_date=None, exit_price=None, exit_reason="no_forward_data",
                pnl_pct=None, holding_days=None)


# ═════════════════════════════════════════════════════════════════════════════
#  DATA LOADING
# ═════════════════════════════════════════════════════════════════════════════

def load_daily_csvs(data_dir: str) -> dict[str, pd.DataFrame]:
    pattern = os.path.join(data_dir, "*_daily.csv")
    files   = sorted(glob(pattern))
    if not files:
        sys.exit(f"No *_daily.csv files found in: {data_dir}")

    result: dict[str, pd.DataFrame] = {}
    skipped = 0
    required = {"Date", "Open", "High", "Low", "Close", "Volume"}

    for fpath in files:
        ticker = os.path.basename(fpath).replace("_daily.csv", "")
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
            result[ticker] = df[["Open", "High", "Low", "Close", "Volume"]]
        except Exception:
            skipped += 1

    print(f"Loaded {len(result)} tickers  ({skipped} skipped).")
    return result


# ═════════════════════════════════════════════════════════════════════════════
#  CORE BACKTEST ENGINE
# ═════════════════════════════════════════════════════════════════════════════

def run_backtest(
    all_data: dict[str, pd.DataFrame],
    start_date: date,
    end_date: date,
    params: dict,
    min_history: int = 210,
) -> list[dict]:
    """
    Walk-forward backtest using pre-cached indicator arrays.
    For each ticker we binary-search its date index instead of boolean-scanning.
    """
    caches = build_caches(all_data, params)

    # Build sorted unique trading-day index using pandas Index union (fast)
    combined_idx: pd.DatetimeIndex = pd.DatetimeIndex([])
    start_ts = pd.Timestamp(start_date)
    end_ts   = pd.Timestamp(end_date)
    for df in all_data.values():
        combined_idx = combined_idx.union(df.index)
    trading_days = combined_idx[(combined_idx >= start_ts) & (combined_idx <= end_ts)]

    trades: list[dict] = []

    for D in trading_days:
        Dd: date = D.date()
        Dord     = Dd.toordinal()

        for ticker, cache in caches.items():
            # Binary search: find rightmost index <= D
            i = np.searchsorted(cache.dates, D.value, side="right") - 1
            if i < 0 or i < min_history - 1:
                continue

            sig = detect_signal(ticker, cache, i, params)
            if sig is None:
                continue

            # Fill bar = bar at i+1
            fill_bar = i + 1
            if fill_bar >= cache.n:
                trades.append(_row(Dd, ticker, sig, None, None, "no_forward_data", None))
                continue

            next_open = cache.open_[fill_bar]
            next_high = cache.high[fill_bar]
            entry_min = sig["entry_min"]
            entry_max = sig["entry_max"]

            if next_open > entry_max:
                trades.append(_row(Dd, ticker, sig, None, None, "stale_entry", None))
                continue
            if next_high < entry_min:
                trades.append(_row(Dd, ticker, sig, None, None, "not_filled", None))
                continue

            fill_price = float(min(max(next_open, entry_min), entry_max))

            ex = simulate_exit_fast(cache, fill_bar, fill_price,
                                    sig["target"], sig["stop_loss"], Dord)
            trades.append(_row(Dd, ticker, sig, fill_price,
                               ex["exit_date"], ex["exit_reason"],
                               ex["pnl_pct"], ex.get("holding_days")))

    return trades


def _row(
    signal_date: date, ticker: str, sig: dict,
    fill_price: float | None, exit_date: date | None,
    exit_reason: str, pnl_pct: float | None,
    holding_days: int | None = None,
) -> dict:
    return {
        "signal_date":  signal_date.isoformat(),
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
#  SUMMARY STATISTICS
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


def print_stats(stats: dict, label: str = "Mean Reversion Backtest") -> None:
    bar = "=" * 64
    print(f"\n{bar}")
    print(f"  {label}")
    print(bar)
    print(f"  Total signals generated : {stats['total_signals']}")
    print(f"  Filled trades           : {stats['total_filled']}")
    print(f"  Completed trades        : {stats['total_trades']}")
    print(f"  Profitable trades       : {stats['win_trades']}")
    print(f"  Losing trades           : {stats['loss_trades']}")
    if stats["total_trades"]:
        print(f"  Win rate                : {stats['win_rate']}%")
        print(f"  Avg PnL %               : {stats['avg_pnl_pct']}%")
        print(f"  Avg holding days        : {stats['avg_holding_days']} days")
        print(f"  Exits  target={stats['target_hit']}  "
              f"stop={stats['stop_loss']}  timeout={stats['timeout']}")
    print(bar)


# ═════════════════════════════════════════════════════════════════════════════
#  COORDINATE-DESCENT OPTIMISER
#
#  Strategy: instead of evaluating all 243 combinations, we hill-climb.
#  Starting from an initial parameter point we sweep each axis in turn,
#  move to whichever value on that axis gives the best avg_pnl_pct, then
#  move to the next axis.  One full pass over all axes = one "round".
#  We repeat rounds until no axis move improves the score (convergence).
#
#  To avoid local optima we run from multiple starting seeds in parallel
#  (corner points of the grid + the default params), then return the
#  globally best result found across all seeds.
#
#  Typical evaluations: ~40–80 backtests vs 243 for exhaustive search.
#  Score objective = avg_pnl_pct  (what you asked for).
#  Tiebreak         = win_rate    (so we don't just maximise a single lucky trade).
# ═════════════════════════════════════════════════════════════════════════════

def _score_params(
    all_data: dict[str, pd.DataFrame],
    start_date: date,
    end_date: date,
    p: dict,
    min_trades: int = 5,
) -> tuple[float, float, dict]:
    """
    Run one backtest for params `p`.
    Returns (avg_pnl_pct, win_rate, stats).
    Returns (-inf, 0, {}) when trade count is below min_trades.
    """
    try:
        trades = run_backtest(all_data, start_date, end_date, p)
        stats  = compute_stats(trades)
    except Exception:
        return -float("inf"), 0.0, {}
    if stats["total_trades"] < min_trades:
        return -float("inf"), 0.0, stats
    return stats["avg_pnl_pct"], stats["win_rate"], stats


def _coord_descent(
    all_data: dict[str, pd.DataFrame],
    start_date: date,
    end_date: date,
    start_params: dict,
    visited: dict[tuple, tuple],   # shared cache: frozen_params → (pnl, wr)
    label: str = "",
) -> tuple[dict, float, float, dict, int]:
    """
    Run coordinate descent from `start_params`.
    Returns (best_params, best_pnl, best_wr, best_stats, evals_used).
    `visited` is mutated in-place so seeds share cached results.
    """
    current = dict(start_params)
    evals   = 0

    def cached_score(p: dict) -> tuple[float, float, dict]:
        key = tuple(sorted(p.items()))
        if key not in visited:
            pnl, wr, st = _score_params(all_data, start_date, end_date, p)
            visited[key] = (pnl, wr, st)
            evals_box[0] += 1
        return visited[key]

    evals_box = [0]

    # Evaluate starting point
    best_pnl, best_wr, best_stats = cached_score(current)

    improved = True
    while improved:
        improved = False
        for axis, candidates in GRID.items():
            axis_best_pnl  = best_pnl
            axis_best_wr   = best_wr
            axis_best_val  = current[axis]
            axis_best_stats = best_stats

            # Evaluate all candidates on this axis
            for val in candidates:
                if val == current[axis]:
                    continue
                probe = dict(current)
                probe[axis] = val
                pnl, wr, st = cached_score(probe)

                # Primary: higher avg_pnl_pct; tiebreak: higher win_rate
                if (pnl, wr) > (axis_best_pnl, axis_best_wr):
                    axis_best_pnl   = pnl
                    axis_best_wr    = wr
                    axis_best_val   = val
                    axis_best_stats = st

            if axis_best_val != current[axis]:
                current[axis]  = axis_best_val
                best_pnl       = axis_best_pnl
                best_wr        = axis_best_wr
                best_stats     = axis_best_stats
                improved       = True

    evals = evals_box[0]
    if label:
        print(f"  seed {label:12s}  evals={evals:3d}  "
              f"avg_pnl={best_pnl:+.4f}%  win_rate={best_wr:.1f}%  "
              f"params={_fmt_params(current)}")
    return current, best_pnl, best_wr, best_stats, evals


def _fmt_params(p: dict) -> str:
    keys = list(GRID.keys())
    return "  ".join(f"{k}={p[k]}" for k in keys)


def _make_seeds(base_params: dict) -> list[tuple[str, dict]]:
    """
    Generate diverse starting points:
      • default params (centre of the grid)
      • all-low corner  (first value of every grid axis)
      • all-high corner (last value of every grid axis)
      • one random mid-point per axis shifted (picks index 1 if exists)
    """
    seeds: list[tuple[str, dict]] = [("default", dict(base_params))]

    low = dict(base_params)
    for k, vals in GRID.items():
        low[k] = vals[0]
    seeds.append(("all_low", low))

    high = dict(base_params)
    for k, vals in GRID.items():
        high[k] = vals[-1]
    seeds.append(("all_high", high))

    # Mid-point seed: pick middle index for each axis
    mid = dict(base_params)
    for k, vals in GRID.items():
        mid[k] = vals[len(vals) // 2]
    seeds.append(("mid", mid))

    return seeds


def _cd_worker(args: tuple) -> tuple:
    """
    Top-level picklable wrapper for ProcessPoolExecutor.
    Returns (best_params, best_pnl, best_wr, best_stats, evals, label).
    """
    all_data, start_date, end_date, seed_params, label = args
    visited: dict = {}
    best_p, best_pnl, best_wr, best_st, evals = _coord_descent(
        all_data, start_date, end_date, seed_params, visited, label,
    )
    return best_p, best_pnl, best_wr, best_st, evals, label


def grid_search(
    all_data: dict[str, pd.DataFrame],
    start_date: date,
    end_date: date,
    base_params: dict,
    top_n: int = 10,
    workers: int | None = None,
) -> list[dict]:
    """
    Coordinate-descent optimiser with multiple seeds run in parallel.
    Each seed hill-climbs independently; we return the globally best result.
    """
    seeds  = _make_seeds(base_params)
    n_seeds = len(seeds)
    print(f"\nCoordinate-descent optimiser: {n_seeds} seeds  "
          f"(workers={workers or min(n_seeds, os.cpu_count())})  …")
    print(f"  Objective: maximise avg_pnl_pct  (tiebreak: win_rate)")
    print(f"  Grid axes: {list(GRID.keys())}")

    job_args = [
        (all_data, start_date, end_date, sp, lbl)
        for lbl, sp in seeds
    ]

    all_results: list[dict] = []
    total_evals = 0

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_cd_worker, a): a for a in job_args}
        for fut in as_completed(futs):
            best_p, best_pnl, best_wr, best_st, evals, label = fut.result()
            total_evals += evals
            if best_pnl == -float("inf") or not best_st:
                continue
            score = best_pnl  # primary objective is avg_pnl_pct
            row: dict = {
                "score":            round(score, 4),
                "seed":             label,
                "evals":            evals,
            }
            for k in GRID:
                row[k] = best_p[k]
            row.update({
                "total_trades":     best_st.get("total_trades", 0),
                "win_trades":       best_st.get("win_trades", 0),
                "loss_trades":      best_st.get("loss_trades", 0),
                "win_rate":         best_st.get("win_rate", 0.0),
                "avg_pnl_pct":      best_st.get("avg_pnl_pct", 0.0),
                "avg_holding_days": best_st.get("avg_holding_days", 0.0),
            })
            all_results.append(row)

    print(f"\n  Total backtests run: {total_evals}  "
          f"(vs {len(list(itertools.product(*GRID.values())))} exhaustive)")

    # De-duplicate by param set, keep highest score per unique params
    seen: dict[tuple, dict] = {}
    for r in all_results:
        key = tuple(r[k] for k in GRID)
        if key not in seen or r["score"] > seen[key]["score"]:
            seen[key] = r
    deduped = sorted(seen.values(), key=lambda r: r["score"], reverse=True)
    return deduped[:top_n]


def print_grid_results(results: list[dict]) -> None:
    if not results:
        print("\nOptimiser: no valid results (need >= 5 trades per run).")
        return
    bar = "=" * 98
    print(f"\n{bar}")
    print("  COORDINATE-DESCENT OPTIMISER — TOP RESULTS  (objective: avg_pnl_pct)")
    print(bar)
    print(
        f"{'#':>3}  {'avgPnL%':>8}  {'rsi':>5}  {'sup%':>5}  "
        f"{'trnd':>5}  {'vol':>5}  {'stop%':>6}  "
        f"{'trades':>6}  {'wins':>5}  {'loss':>5}  "
        f"{'win%':>6}  {'hold':>5}  {'evals':>5}  {'seed':<10}"
    )
    print("-" * 98)
    for rank, r in enumerate(results, 1):
        print(
            f"{rank:>3}  {r['avg_pnl_pct']:>8.4f}  "
            f"{r['rsi_max']:>5}  {r['support_tolerance']:>5}  "
            f"{r['trend_break_buffer']:>5}  {r['min_volume_ratio']:>5}  "
            f"{r['stop_pct']:>6}  "
            f"{r['total_trades']:>6}  {r['win_trades']:>5}  {r['loss_trades']:>5}  "
            f"{r['win_rate']:>6.1f}  {r['avg_holding_days']:>5.1f}  "
            f"{r.get('evals', '?'):>5}  {r.get('seed', ''):10}"
        )
    print(bar)
    best = results[0]
    print("\n  BEST PARAMETERS  (highest avg_pnl_pct):")
    for k in GRID:
        print(f"    {k:25s} = {best[k]}")
    print(f"    {'avg_pnl_pct':25s} = {best['avg_pnl_pct']:+.4f}%")
    print(f"    {'win_rate':25s} = {best['win_rate']:.1f}%")
    print(f"    {'total_trades':25s} = {best['total_trades']}")
    print()


# ═════════════════════════════════════════════════════════════════════════════
#  CSV OUTPUT
# ═════════════════════════════════════════════════════════════════════════════

def write_outputs(
    out_dir: str,
    trades: list[dict],
    grid_results: list[dict],
    label: str,
) -> None:
    from datetime import datetime
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if trades:
        path = os.path.join(out_dir, f"mr_trades_{label}_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)
        print(f"Trades CSV : {path}")

    if grid_results:
        path = os.path.join(out_dir, f"mr_grid_{label}_{ts}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(grid_results[0].keys()))
            w.writeheader()
            w.writerows(grid_results)
        print(f"Grid CSV   : {path}")


# ═════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Mean Reversion backtest + grid search on daily CSVs."
    )
    ap.add_argument(
        "--data-dir",
        default=os.path.join(os.path.dirname(os.path.dirname(_HERE)), "daily_data"),
        help="Directory containing *_daily.csv files",
    )
    ap.add_argument("--days",    type=int, default=180)
    ap.add_argument("--top-n",  type=int, default=10)
    ap.add_argument("--workers", type=int, default=None,
                    help="Parallel workers for grid search (default: CPU count)")
    ap.add_argument("--no-grid", action="store_true",
                    help="Skip grid search")
    ap.add_argument(
        "--out-dir",
        default=os.path.join(_HERE, "reports"),
    )
    args = ap.parse_args()

    end_date   = date.today()
    start_date = end_date - timedelta(days=args.days)
    print(f"Backtest window : {start_date}  →  {end_date}  ({args.days} days)")

    all_data = load_daily_csvs(args.data_dir)

    print("\nRunning baseline backtest …")
    trades = run_backtest(all_data, start_date, end_date, DEFAULT_PARAMS)
    stats  = compute_stats(trades)
    print_stats(stats, label=f"Mean Reversion — last {args.days} days (default params)")

    grid_results: list[dict] = []
    if not args.no_grid:
        grid_results = grid_search(
            all_data, start_date, end_date, DEFAULT_PARAMS,
            top_n=args.top_n, workers=args.workers,
        )
        print_grid_results(grid_results)

    write_outputs(args.out_dir, trades, grid_results, f"{args.days}d")


if __name__ == "__main__":
    main()
