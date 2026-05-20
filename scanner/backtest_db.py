"""
Walk-forward backtest using `stocks.daily_ohlcv` only (no Yahoo).

Runs breakout, EMA pullback, VCP, mean reversion, Fib pullback and Fear Reversion.
RS Resilience is skipped (requires Nifty OHLCV in DB).
Fear Reversion fetches ^INDIAVIX from yfinance once at startup (small call, ~2s).

Usage (from `scanner/` directory):
  python backtest_db.py
  python backtest_db.py --from 2024-01-01 --to 2025-03-31
  python backtest_db.py --out-dir ./backtest_reports

Requires DATABASE_URL in .env (read-only SELECT on stocks.daily_ohlcv).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from statistics import median
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

import yfinance as yf
from db import get_connection
from ema_scanner import analyse_ema_pullback
from fib_pullback_scanner import analyse_fib_pullback
from fear_reversion_scanner import analyse_fear_reversion, LARGE_CAP_SYMBOLS, VIX_THRESHOLD
from mean_reversion_scanner import analyse_mean_reversion
from performance import INVESTMENT, MAX_HOLD_DAYS, simulate_trade_exit
from stocks import STOCKS
from vcp_scanner import analyse_vcp

from main import TOP_N, analyse_breakout, LOOKBACK_DAYS

MIN_HISTORY = LOOKBACK_DAYS  # 220 — covers VCP / 200 EMA / mean reversion

OHLCV_SQL = """
    SELECT ticker, date, open, high, low, close, volume
    FROM stocks.daily_ohlcv
    WHERE ticker = ANY(%s::text[])
      AND date >= %s
      AND date <= %s
    ORDER BY ticker, date
"""


def resolve_stock_to_db_ticker(db_tickers: set[str]) -> dict[str, str]:
    """Map each STOCKS entry to the matching `daily_ohlcv.ticker` row (exact or bare NSE)."""
    out: dict[str, str] = {}
    for s in STOCKS:
        if s in db_tickers:
            out[s] = s
            continue
        base = s.replace(".NS", "").strip()
        if base in db_tickers:
            out[s] = base
    return out


def load_distinct_tickers(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ticker FROM stocks.daily_ohlcv")
        return {str(r[0]).strip() for r in cur.fetchall()}


def load_ohlcv(
    conn,
    stock_to_db: dict[str, str],
    d_from: date,
    d_to: date,
) -> dict[str, pd.DataFrame]:
    """One DataFrame per logical stock key (Yahoo-style ticker used in scanner)."""
    db_ticker_list = sorted(set(stock_to_db.values()))
    if not db_ticker_list:
        return {}

    db_to_stock: dict[str, str] = {}
    for stock_sym, db_t in stock_to_db.items():
        db_to_stock[db_t] = stock_sym

    with conn.cursor() as cur:
        cur.execute(OHLCV_SQL, (db_ticker_list, d_from, d_to))
        rows = cur.fetchall()

    if not rows:
        return {}

    df = pd.DataFrame(
        rows,
        columns=["ticker", "date", "open", "high", "low", "close", "volume"],
    )
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()

    result: dict[str, pd.DataFrame] = {}
    for db_t, g in df.groupby("ticker"):
        stock_key = db_to_stock.get(db_t)
        if not stock_key:
            continue
        o = g.set_index("date").sort_index()
        ohlc = pd.DataFrame(
            {
                "Open": pd.to_numeric(o["open"], errors="coerce"),
                "High": pd.to_numeric(o["high"], errors="coerce"),
                "Low": pd.to_numeric(o["low"], errors="coerce"),
                "Close": pd.to_numeric(o["close"], errors="coerce"),
                "Volume": pd.to_numeric(o["volume"], errors="coerce"),
            }
        )
        ohlc = ohlc.dropna(how="any")
        ohlc = ohlc[~ohlc.index.duplicated(keep="last")]
        if not ohlc.empty:
            result[stock_key] = ohlc

    return result


def _to_date(d: pd.Timestamp) -> date:
    return d.date() if hasattr(d, "date") else date.fromisoformat(str(d)[:10])


def future_bars_after(full_df: pd.DataFrame, signal_date: date) -> list[tuple[date, float, float, float, float]]:
    """Return (date, open, high, low, close) tuples for bars strictly after signal_date."""
    ts = pd.Timestamp(signal_date)
    sub = full_df[full_df.index > ts]
    out: list[tuple[date, float, float, float, float]] = []
    for idx, row in sub.iterrows():
        out.append(
            (
                _to_date(idx),
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
            )
        )
    return out


def determine_fill(
    first_bar: tuple[date, float, float, float, float],
    entry_min: float,
    entry_max: float,
) -> tuple[str, float | None]:
    """
    Decide whether the next bar after signal day actually fills the trade.

    Returns (status, fill_price) where status is one of:
      filled       — fill at clamp(open, entry_min, entry_max)
      stale_entry  — gap above entry_max; chasing price, skip
      not_filled   — bar's high never reached entry_min; never triggered
    """
    _, b_open, b_high, _, _ = first_bar
    if b_open > entry_max:
        return "stale_entry", None
    if b_high < entry_min:
        return "not_filled", None
    fill_price = min(max(b_open, entry_min), entry_max)
    return "filled", fill_price


def run_backtest(
    d_from: date,
    d_to: date,
    out_dir: str,
) -> tuple[list[dict], list[dict]]:
    conn = get_connection()
    try:
        db_all = load_distinct_tickers(conn)
        stock_to_db = resolve_stock_to_db_ticker(db_all)
        if not stock_to_db:
            print("No overlap between STOCKS and stocks.daily_ohlcv tickers.", file=sys.stderr)
            sys.exit(1)

        print(f"Universe: {len(stock_to_db)} stock(s) matched in DB.")

        # Load extra history before d_from so scanners have >= MIN_HISTORY bars
        # available on the first signal day in the window.
        from datetime import timedelta as _td
        history_buffer = _td(days=int(MIN_HISTORY * 1.6) + 30)  # calendar days for ~MIN_HISTORY trading bars
        load_from = d_from - history_buffer
        data = load_ohlcv(conn, stock_to_db, load_from, d_to)
    finally:
        conn.close()

    if not data:
        print("No OHLCV rows in range — check dates and tickers.", file=sys.stderr)
        sys.exit(1)

    all_dates = sorted({d for df in data.values() for d in df.index})
    all_dates = [pd.Timestamp(d).normalize() for d in all_dates]
    all_dates = sorted(set(all_dates))
    all_dates = [d for d in all_dates if d_from <= _to_date(d) <= d_to]

    first_possible: list[pd.Timestamp] = []
    for df in data.values():
        if len(df) >= MIN_HISTORY:
            first_possible.append(df.index[MIN_HISTORY - 1])
    if not first_possible:
        print(f"No symbol has at least {MIN_HISTORY} trading days in range.", file=sys.stderr)
        sys.exit(1)
    global_start = max(min(first_possible), pd.Timestamp(d_from))

    all_dates = [d for d in all_dates if d >= global_start]

    # Fetch India VIX once for Fear Reversion regime filter
    print("Fetching ^INDIAVIX for Fear Reversion backtest window...", flush=True)
    try:
        from datetime import timedelta as _tdvix
        vix_raw = yf.download(
            "^INDIAVIX",
            start=(d_from - _tdvix(days=5)).isoformat(),
            end=(d_to + _tdvix(days=5)).isoformat(),
            interval="1d", auto_adjust=True, progress=False,
        )
        if isinstance(vix_raw.columns, pd.MultiIndex):
            vix_raw.columns = vix_raw.columns.get_level_values(0)
        # Build set of dates where VIX was elevated
        vix_elevated_dates: set[date] = set()
        for idx, row in vix_raw.iterrows():
            if float(row["Close"]) > VIX_THRESHOLD:
                vix_elevated_dates.add(_to_date(idx))
        print(f"VIX elevated (>{VIX_THRESHOLD}) on {len(vix_elevated_dates)} days in range.")
    except Exception as e:
        print(f"VIX fetch failed ({e}) — Fear Reversion will be skipped.", file=sys.stderr)
        vix_elevated_dates = set()

    strategy_names = ("breakout", "ema_pullback", "vcp", "mean_reversion", "fib_pullback", "fear_reversion")

    trades: list[dict] = []

    for D in all_dates:
        Dd = _to_date(D)
        vix_high_today = Dd in vix_elevated_dates
        pools: dict[str, list[tuple[dict, str]]] = defaultdict(list)

        for stock_key, full_df in data.items():
            sl = full_df[full_df.index <= D]
            if len(sl) < MIN_HISTORY:
                continue

            b = analyse_breakout(stock_key, sl)
            e = analyse_ema_pullback(stock_key, sl)
            v = analyse_vcp(stock_key, sl)
            m = analyse_mean_reversion(stock_key, sl)
            f = analyse_fib_pullback(stock_key, sl)
            fr = analyse_fear_reversion(stock_key, sl) if vix_high_today else None

            if b:
                pools["breakout"].append((b, stock_key))
            if e:
                pools["ema_pullback"].append((e, stock_key))
            if v:
                pools["vcp"].append((v, stock_key))
            if m:
                pools["mean_reversion"].append((m, stock_key))
            if f:
                pools["fib_pullback"].append((f, stock_key))
            if fr:
                pools["fear_reversion"].append((fr, stock_key))

        for strat_name in strategy_names:
            lst = pools[strat_name]
            lst.sort(key=lambda x: x[0]["volume_ratio"], reverse=True)
            for sig, sk in lst[:TOP_N]:
                full_df = data[sk]

                fut = future_bars_after(full_df, Dd)
                entry_min = float(sig["entry_min"])
                entry_max = float(sig["entry_max"])
                target = float(sig["target"])
                stop_loss = float(sig["stop_loss"])

                row = {
                    "signal_date": Dd.isoformat(),
                    "strategy": strat_name,
                    "symbol": sig["symbol"],
                    "ticker": sk,
                    "cmp": sig["cmp"],
                    "entry_min": entry_min,
                    "entry_max": entry_max,
                    "target": target,
                    "stop_loss": stop_loss,
                    "volume_ratio": sig["volume_ratio"],
                    "rsi": sig["rsi"],
                    "fill_price": "",
                    "exit_date": "",
                    "exit_price": "",
                    "exit_reason": "",
                    "pnl": "",
                    "pnl_pct": "",
                }

                if not fut:
                    row["exit_reason"] = "no_forward_data"
                    trades.append(row)
                    continue

                status, fill_price = determine_fill(fut[0], entry_min, entry_max)
                if status != "filled":
                    row["exit_reason"] = status  # stale_entry or not_filled
                    trades.append(row)
                    continue

                # Strip open for the simulator (it only needs date/high/low/close).
                sim_bars = [(d, h, l, c) for (d, _o, h, l, c) in fut]
                ex = simulate_trade_exit(
                    Dd,
                    sim_bars,
                    float(fill_price),
                    target,
                    stop_loss,
                    MAX_HOLD_DAYS,
                    INVESTMENT,
                )
                row["fill_price"] = round(float(fill_price), 4)
                row["exit_date"] = ex["exit_date"].isoformat() if ex["exit_date"] else ""
                row["exit_price"] = ex["exit_price"] if ex["exit_price"] is not None else ""
                row["exit_reason"] = ex["exit_reason"]
                row["pnl"] = ex["pnl"] if ex["pnl"] is not None else ""
                row["pnl_pct"] = ex["pnl_pct"] if ex["pnl_pct"] is not None else ""
                trades.append(row)

    summary = build_summary(trades)
    write_reports(out_dir, trades, summary)
    return trades, summary


def build_summary(trades: list[dict]) -> list[dict]:
    natural = {"target_hit", "stop_loss", "timeout"}
    by_strat: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_strat[t["strategy"]].append(t)

    summary: list[dict] = []
    for strat in sorted(by_strat.keys()):
        rows = by_strat[strat]
        n = len(rows)
        natural_rows = [r for r in rows if r["exit_reason"] in natural]
        pnl_vals = [
            float(r["pnl_pct"])
            for r in natural_rows
            if r["pnl_pct"] != "" and r["pnl_pct"] is not None
        ]
        wins = sum(1 for x in pnl_vals if x > 0)
        reasons = defaultdict(int)
        for r in rows:
            reasons[r["exit_reason"]] += 1

        total_pnl = sum(
            float(r["pnl"]) for r in rows if r["pnl"] != "" and r["pnl"] is not None
        )

        summary.append(
            {
                "strategy": strat,
                "n_signals": n,
                "n_filled": len(natural_rows) + reasons["eod_sample"],
                "n_not_filled": reasons["not_filled"],
                "n_stale_entry": reasons["stale_entry"],
                "n_natural_exit": len(natural_rows),
                "n_eod_sample": reasons["eod_sample"],
                "n_no_forward_data": reasons["no_forward_data"],
                "win_rate_natural_pct": round(100.0 * wins / len(pnl_vals), 2) if pnl_vals else "",
                "avg_pnl_pct_natural": round(sum(pnl_vals) / len(pnl_vals), 4) if pnl_vals else "",
                "median_pnl_pct_natural": round(median(pnl_vals), 4) if pnl_vals else "",
                "total_pnl_inr_all": round(total_pnl, 2),
                "n_target_hit": reasons["target_hit"],
                "n_stop_loss": reasons["stop_loss"],
                "n_timeout": reasons["timeout"],
            }
        )
    return summary


def write_reports(out_dir: str, trades: list[dict], summary: list[dict]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    trades_path = os.path.join(out_dir, f"trades_{ts}.csv")
    summary_path = os.path.join(out_dir, f"summary_{ts}.csv")

    t_fields = [
        "signal_date",
        "strategy",
        "symbol",
        "ticker",
        "cmp",
        "entry_min",
        "entry_max",
        "target",
        "stop_loss",
        "volume_ratio",
        "rsi",
        "fill_price",
        "exit_date",
        "exit_price",
        "exit_reason",
        "pnl",
        "pnl_pct",
    ]
    with open(trades_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=t_fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(trades)

    s_fields = list(summary[0].keys()) if summary else []
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        if summary:
            w = csv.DictWriter(f, fieldnames=s_fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(summary)
        else:
            f.write("strategy,n_trades\n")

    if not summary:
        print("\nNo trades generated in this range (check data length vs MIN_HISTORY).")
    else:
        print("\n--- Backtest summary (natural exits = target_hit + stop_loss + timeout) ---")
        print("Win rate & avg PnL % are over natural exits only; eod_sample / no_forward_data excluded.\n")
        for s in summary:
            print(f"  {s['strategy']}")
            print(f"    signals={s['n_signals']}  filled={s['n_filled']}  "
                  f"not_filled={s['n_not_filled']}  stale={s['n_stale_entry']}")
            print(f"    natural_exits={s['n_natural_exit']}  eod_sample={s['n_eod_sample']}  "
                  f"no_forward={s['n_no_forward_data']}")
            print(f"    target / SL / timeout: {s['n_target_hit']} / {s['n_stop_loss']} / {s['n_timeout']}")
            if s["win_rate_natural_pct"] != "":
                print(f"    win_rate (pnl>0 | natural): {s['win_rate_natural_pct']}%")
                print(f"    avg pnl % (natural): {s['avg_pnl_pct_natural']}  "
                      f"median: {s['median_pnl_pct_natural']}")
            print(f"    total pnl (all exits, Rs {INVESTMENT} notional each): Rs {s['total_pnl_inr_all']}")
            print()

    print(f"Wrote: {trades_path}")
    print(f"Wrote: {summary_path}")
    print("\nNote: RS Resilience is not backtested (no Nifty series in DB).")
    print("Fear Reversion uses ^INDIAVIX fetched from yfinance — regime filter applied per day.")


def parse_date(s: str) -> date:
    return date.fromisoformat(s.strip())


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest from stocks.daily_ohlcv (no Yahoo).")
    ap.add_argument("--from", dest="d_from", type=str, default=None, help="Start date YYYY-MM-DD")
    ap.add_argument("--to", dest="d_to", type=str, default=None, help="End date YYYY-MM-DD")
    ap.add_argument(
        "--out-dir",
        default=os.path.join(os.path.dirname(__file__), "backtest_reports"),
        help="Directory for trades_*.csv and summary_*.csv",
    )
    args = ap.parse_args()

    if "DATABASE_URL" not in os.environ:
        print("DATABASE_URL missing in environment.", file=sys.stderr)
        sys.exit(1)

    conn = get_connection()
    try:
        db_all = load_distinct_tickers(conn)
        stock_to_db = resolve_stock_to_db_ticker(db_all)
        if not stock_to_db:
            print("No STOCKS tickers found in stocks.daily_ohlcv.", file=sys.stderr)
            sys.exit(1)
        db_keys = sorted(set(stock_to_db.values()))

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MIN(date)::text, MAX(date)::text
                FROM stocks.daily_ohlcv
                WHERE ticker = ANY(%s::text[])
                """,
                (db_keys,),
            )
            rmin, rmax = cur.fetchone()
        if not rmin or not rmax:
            print("No rows in stocks.daily_ohlcv for matched universe tickers.", file=sys.stderr)
            sys.exit(1)
        d_lo = parse_date(rmin)
        d_hi = parse_date(rmax)
    finally:
        conn.close()

    d_from = parse_date(args.d_from) if args.d_from else d_lo
    d_to = parse_date(args.d_to) if args.d_to else d_hi
    if d_from > d_to:
        print("--from must be <= --to", file=sys.stderr)
        sys.exit(1)

    print(f"Date range: {d_from} .. {d_to}")
    run_backtest(d_from, d_to, args.out_dir)


if __name__ == "__main__":
    main()
