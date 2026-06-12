"""
Last-3-weeks paper-trade review — RS vs EMA (and all strategies).

Reads swing.strategy_performance and breaks down performance over the last
21 days by strategy: win rate, avg/median pnl%, total INR, and exit-reason mix.
Closed trades drive win-rate; open trades are shown separately (not yet resolved).

Run from the scanner folder (where .env lives):
    python rs_vs_ema_3weeks.py
    python rs_vs_ema_3weeks.py --days 21
"""

import os, sys, argparse
from datetime import date, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import dotenv_values

cfg = dotenv_values(os.path.join(os.path.dirname(__file__), ".env"))
DB_URL = cfg.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
SCHEMA = cfg.get("DB_SCHEMA", "swing")
TABLE  = "strategy_performance"


def fetch(days: int):
    since = date.today() - timedelta(days=days)
    conn = psycopg2.connect(DB_URL)
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT strategy, symbol, signal_date, exit_date, status,
                   exit_reason, entry_price, exit_price, pnl, pnl_pct, investment
            FROM {SCHEMA}.{TABLE}
            WHERE signal_date >= %s
            ORDER BY strategy, signal_date
            """,
            (since,),
        )
        rows = cur.fetchall()
    conn.close()
    return rows, since


def f(x, d=2):
    return f"{float(x):.{d}f}" if x is not None else "—"


def summarise(rows, since, days):
    strategies = sorted({r["strategy"] for r in rows})
    print(f"\n{'='*70}")
    print(f"  PAPER-TRADE REVIEW  —  last {days} days  (since {since})  |  {len(rows)} signals")
    print(f"{'='*70}")

    header = f"{'strategy':<16}{'sigs':>5}{'closed':>7}{'open':>5}{'win%':>7}{'avg%':>8}{'med%':>8}{'totINR':>9}{'tgt':>5}{'sl':>4}{'to':>4}"
    print("\n" + header)
    print("-" * len(header))

    for strat in strategies:
        srows  = [r for r in rows if r["strategy"] == strat]
        closed = [r for r in srows if r["status"] == "closed"]
        opn    = [r for r in srows if r["status"] != "closed"]
        wins   = [r for r in closed if r["pnl_pct"] is not None and float(r["pnl_pct"]) > 0]
        pnls   = sorted(float(r["pnl_pct"]) for r in closed if r["pnl_pct"] is not None)
        avg    = sum(pnls) / len(pnls) if pnls else None
        med    = pnls[len(pnls)//2] if pnls else None
        tot    = sum(float(r["pnl"]) for r in closed if r["pnl"] is not None)
        win_pct = (len(wins) / len(closed) * 100) if closed else None
        tgt = sum(1 for r in closed if r["exit_reason"] == "target_hit")
        sl  = sum(1 for r in closed if r["exit_reason"] == "stop_loss")
        to  = sum(1 for r in closed if r["exit_reason"] == "timeout")
        print(f"{strat:<16}{len(srows):>5}{len(closed):>7}{len(opn):>5}"
              f"{f(win_pct,1):>7}{f(avg):>8}{f(med):>8}{f(tot,0):>9}{tgt:>5}{sl:>4}{to:>4}")

    # Detailed RS vs EMA trade list
    for strat in ("rs_resilience", "ema_pullback"):
        srows = [r for r in rows if r["strategy"] == strat]
        if not srows:
            continue
        print(f"\n  ── {strat} — every trade ──")
        print(f"  {'symbol':<14}{'signal':<12}{'exit':<12}{'status':<8}{'reason':<12}{'pnl%':>8}{'pnlINR':>9}")
        for r in srows:
            print(f"  {r['symbol']:<14}{str(r['signal_date']):<12}"
                  f"{str(r['exit_date'] or ''):<12}{r['status']:<8}"
                  f"{(r['exit_reason'] or ''):<12}{f(r['pnl_pct']):>8}{f(r['pnl'],0):>9}")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=21)
    args = ap.parse_args()
    if not DB_URL:
        sys.exit("DATABASE_URL not found in .env")
    rows, since = fetch(args.days)
    if not rows:
        print(f"No signals in the last {args.days} days.")
    else:
        summarise(rows, since, args.days)
