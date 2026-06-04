"""
Backtest: RS Resilience entry strategy comparison
  A) Next-day morning — enter at next open (skip if gapped > 1.5%)
  B) Mid-day proxy    — enter at signal day's close (~1pm price proxy)

Walks forward through 250 trading days of history, fires the same
RS filters EOD, then checks next-day open and outcome over 15 days.
"""

import sys, time
import yfinance as yf
import pandas as pd

sys.path.insert(0, ".")
from indicators import calculate_rsi, calculate_ema, mansfield_rs
from stocks import STOCKS, BATCH_SIZE

# ── Parameters (must mirror rs_scanner.py) ───────────────────────────────────
NIFTY_EMA           = 20
NIFTY_DROP_LOOKBACK = 10
NIFTY_MAX_GAIN      = 0.01
RS_OUTPERFORM_PP    = 5.0
STOCK_EMA           = 50
HIGHER_LOW_LOOKBACK = 20
HL_WINDOW           = 10
RSI_MIN             = 45
RSI_MAX             = 65
TARGET_PCT          = 0.06
STOP_PCT            = 0.03
MANSFIELD_LOOKBACK  = 5
MIN_ROWS_NEEDED     = STOCK_EMA + HIGHER_LOW_LOOKBACK + 10

# ── Backtest config ───────────────────────────────────────────────────────────
BACKTEST_DAYS   = 250     # walk-forward window
TOTAL_DAYS      = 220 + BACKTEST_DAYS + 30
SIM_WINDOW      = 15      # days to look for target/stop hit
MORNING_GAP_MAX = 0.015   # allow up to 1.5% gap for morning entry
NIFTY_TICKER    = "^NSEI"
BATCH_DELAY_SEC = 1
# ─────────────────────────────────────────────────────────────────────────────


def rs_signal_at(c, l, n):
    """Run RS filters on pre-sliced series. Returns signal dict or None."""
    if len(c) < MIN_ROWS_NEEDED or len(n) < NIFTY_DROP_LOOKBACK + 10:
        return None

    nifty_10d_ret = (float(n.iloc[-1]) - float(n.iloc[-(NIFTY_DROP_LOOKBACK+1)])) / float(n.iloc[-(NIFTY_DROP_LOOKBACK+1)])
    if nifty_10d_ret >= NIFTY_MAX_GAIN:
        return None

    cmp           = float(c.iloc[-1])
    stock_10d_ret = (cmp - float(c.iloc[-(NIFTY_DROP_LOOKBACK+1)])) / float(c.iloc[-(NIFTY_DROP_LOOKBACK+1)]) * 100
    if (stock_10d_ret - nifty_10d_ret * 100) < RS_OUTPERFORM_PP:
        return None

    rs_now = mansfield_rs(c, n, period=NIFTY_EMA)
    rs_5d  = mansfield_rs(c.iloc[:-MANSFIELD_LOOKBACK], n.iloc[:-MANSFIELD_LOOKBACK], period=NIFTY_EMA)
    if rs_now <= rs_5d:
        return None

    if cmp <= float(calculate_ema(c, STOCK_EMA).iloc[-1]):
        return None

    low_now  = float(l.iloc[-HL_WINDOW:].min())
    low_back = float(l.iloc[-(HIGHER_LOW_LOOKBACK+HL_WINDOW):-HIGHER_LOW_LOOKBACK].min())
    if low_now <= low_back:
        return None

    if not (RSI_MIN <= calculate_rsi(c) <= RSI_MAX):
        return None

    entry_min = cmp * 0.99
    ema20     = float(calculate_ema(c, NIFTY_EMA).iloc[-1])
    stop      = round(max(ema20 * (1 - STOP_PCT), float(l.iloc[-HL_WINDOW:].min())), 2)
    target    = round(entry_min * (1 + TARGET_PCT), 2)

    if stop >= entry_min:
        return None

    return {"cmp": cmp, "target": target, "stop": stop}


def trade_outcome(high_fwd, low_fwd, target, stop):
    """Return (result, days) — first of target/stop hit within SIM_WINDOW."""
    for j in range(min(SIM_WINDOW, len(high_fwd))):
        h, l = float(high_fwd.iloc[j]), float(low_fwd.iloc[j])
        if h >= target and l <= stop:
            return "ambiguous", j + 1
        if h >= target:
            return "win", j + 1
        if l <= stop:
            return "loss", j + 1
    return "open", SIM_WINDOW


def main():
    print(f"Downloading Nifty ({TOTAL_DAYS}d)...")
    nifty_raw = yf.download(NIFTY_TICKER, period=f"{TOTAL_DAYS}d", interval="1d",
                             auto_adjust=True, progress=False)
    if isinstance(nifty_raw.columns, pd.MultiIndex):
        nifty_raw.columns = nifty_raw.columns.get_level_values(0)
    nifty_close = nifty_raw["Close"].dropna()

    records = []
    batches = [STOCKS[i:i+BATCH_SIZE] for i in range(0, len(STOCKS), BATCH_SIZE)]

    for b_num, batch in enumerate(batches, 1):
        print(f"[Batch {b_num}/{len(batches)}] Downloading {len(batch)} stocks...", flush=True)
        raw = yf.download(
            " ".join(batch), period=f"{TOTAL_DAYS}d", interval="1d",
            group_by="ticker", auto_adjust=True, progress=False, threads=True,
        )

        for sym in batch:
            try:
                df = raw.copy() if len(batch) == 1 else (
                    raw[sym].copy() if sym in raw.columns.get_level_values(0) else pd.DataFrame()
                )
                if df.empty or len(df) < MIN_ROWS_NEEDED + SIM_WINDOW + 5:
                    continue

                close = df["Close"].dropna()
                high  = df["High"].dropna()
                low   = df["Low"].dropna()
                opens = df["Open"].dropna()
                nc    = nifty_close.reindex(close.index, method="ffill").dropna()

                scan_start = max(MIN_ROWS_NEEDED, len(close) - BACKTEST_DAYS - SIM_WINDOW)
                scan_end   = len(close) - SIM_WINDOW - 1

                for i in range(scan_start, scan_end):
                    sig = rs_signal_at(close.iloc[:i+1], low.iloc[:i+1], nc.iloc[:i+1])
                    if sig is None:
                        continue

                    cmp        = sig["cmp"]
                    target     = sig["target"]
                    stop       = sig["stop"]
                    h_fwd      = high.iloc[i+1:]
                    l_fwd      = low.iloc[i+1:]
                    next_open  = float(opens.iloc[i+1]) if i + 1 < len(opens) else cmp
                    gap_pct    = (next_open - cmp) / cmp * 100

                    # Morning entry: fill if next open within gap limit
                    if next_open <= cmp * (1 + MORNING_GAP_MAX):
                        mor_out, mor_days = trade_outcome(h_fwd, l_fwd, target, stop)
                    else:
                        mor_out, mor_days = "missed", 0

                    # Mid-day proxy: always fills at signal-day close
                    md_out, md_days = trade_outcome(h_fwd, l_fwd, target, stop)

                    records.append({
                        "symbol":   sym,
                        "gap_pct":  round(gap_pct, 2),
                        "mor_out":  mor_out,
                        "mor_days": mor_days,
                        "md_out":   md_out,
                        "md_days":  md_days,
                        "rr":       round((target - cmp) / (cmp - stop), 2) if cmp > stop else 0,
                    })
            except Exception:
                pass

        if b_num < len(batches):
            time.sleep(BATCH_DELAY_SEC)

    if not records:
        print("No signals found in backtest period.")
        return

    df = pd.DataFrame(records)
    n  = len(df)

    filled   = df[df["mor_out"] != "missed"]
    missed   = df[df["mor_out"] == "missed"]
    m_win    = filled[filled["mor_out"] == "win"]
    m_loss   = filled[filled["mor_out"] == "loss"]
    m_open   = filled[filled["mor_out"] == "open"]
    md_win   = df[df["md_out"] == "win"]
    md_loss  = df[df["md_out"] == "loss"]
    md_open  = df[df["md_out"] == "open"]

    def pct(a, b): return f"{a/b*100:.0f}%" if b else "—"
    def avg_days(s): return f"avg {s['mor_days'].mean():.1f}d" if len(s) else ""
    def avg_days_md(s): return f"avg {s['md_days'].mean():.1f}d" if len(s) else ""

    print(f"\n{'='*62}")
    print(f"  RS ENTRY BACKTEST  —  {BACKTEST_DAYS} trading days  |  {n} total signals")
    print(f"{'='*62}")
    print()
    print(f"  A) NEXT-DAY MORNING  (enter at open, max gap +{MORNING_GAP_MAX*100:.1f}%)")
    print(f"     Filled     {len(filled):>4} / {n}  ({pct(len(filled),n)})")
    print(f"     Gapped out {len(missed):>4} / {n}  ({pct(len(missed),n)})")
    if len(filled):
        print(f"     Win        {len(m_win):>4} / {len(filled)}  ({pct(len(m_win),len(filled))})  {avg_days(m_win)}")
        print(f"     Loss       {len(m_loss):>4} / {len(filled)}  ({pct(len(m_loss),len(filled))})  {avg_days(m_loss)}")
        print(f"     Open       {len(m_open):>4} / {len(filled)}")
    print()
    print(f"  B) MID-DAY PROXY  (signal-day close ~1pm price)")
    print(f"     Filled     {n:>4} / {n}  (100%)")
    print(f"     Win        {len(md_win):>4} / {n}  ({pct(len(md_win),n)})  {avg_days_md(md_win)}")
    print(f"     Loss       {len(md_loss):>4} / {n}  ({pct(len(md_loss),n)})  {avg_days_md(md_loss)}")
    print(f"     Open       {len(md_open):>4} / {n}")
    print()
    print(f"  Gap distribution (next open vs signal-day close):")
    print(f"     Average gap      {df['gap_pct'].mean():+.2f}%")
    print(f"     Gapped up > 1.5% {len(df[df['gap_pct']>1.5]):>4} / {n}  ({pct(len(df[df['gap_pct']>1.5]),n)})")
    print(f"     Gapped up > 3.0% {len(df[df['gap_pct']>3.0]):>4} / {n}  ({pct(len(df[df['gap_pct']>3.0]),n)})")
    print(f"     Gapped down      {len(df[df['gap_pct']<0]):>4} / {n}  ({pct(len(df[df['gap_pct']<0]),n)})")
    print()
    print(f"  Avg R:R (signal)   {df['rr'].mean():.2f}")
    print(f"{'='*62}")


if __name__ == "__main__":
    main()
