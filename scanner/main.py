"""
Daily Buy-Signal Orchestrator (post-backtest winning strategies only).

Backtest (Jan-May 2026) winners kept:
   #  Strategy [Hero]                  Trades  sig/yr  Avg PnL%   Total Rs
   1  s05_garch_volume [GARudaVahana]    104    267     +3.94%   +40,159
   2  s07_wavelet_volume [Wayuputra]     167    429     +1.87%   +30,587
   3  sanjay_xgb_b8 [Gobin xood]         137    352     +2.06%   +28,246
   4  s08_gap_momentum [GaMomra]          12     31     +7.50%    +9,000
   5  s06_tcn_ohlcv [TeCNa]              132    339     +0.46%    +6,092
   6  s11_cluster_meanrev [KlaMeReous]    30     77     +1.77%    +5,300
   7  swing_mr [MiRana]                   48    123     +0.75%    +3,616

Excluded: swing_rs (-29,714), swing_ema, swing_breakout, swing_fib, swing_vcp, swing_fr.

Pipeline (when called as `python main.py`):
  1. daily_suggestor.run_daily.main()
       - refreshes daily_data via batched yfinance (BATCH_SIZE=100, group_by=ticker)
       - resolves open positions
       - scores 6 ML strategies, dedupes, saves OPEN_PENDING_FILL trades
       - sends ntfy with buys/sells
  2. Inline swing_mr scan AGAINST THE FRESHLY-REFRESHED LOCAL CSVs
       - reuses daily_suggestor's universe (~1700 tickers, not just Nifty 200)
       - no second yfinance download
       - saves to swing.mean_reversion_signals (existing table)
  3. Summary ntfy with per-strategy counts + capital allocation

The legacy 7-strategy scanner lives at main_sanjay.py — this is what the
DigitalOcean cron runs (see scanner/run.sh).

main.py is for LOCAL use only when daily_suggestor is checked out alongside
this repo (writes daily_suggestor.trades + swing_mr). It is NOT used on the droplet.
"""
import os
import sys
from pathlib import Path

# Make daily_suggestor importable (separate repo, NOT part of swing-trade)
_HERE = Path(__file__).resolve().parent
_DEFAULT_DS = _HERE.parent.parent / "daily_suggestor"
_DAILY_SUGGESTOR = Path(os.environ.get("DAILY_SUGGESTOR_DIR", _DEFAULT_DS)).resolve()
_RUN_DAILY = _DAILY_SUGGESTOR / "run_daily.py"

if not _RUN_DAILY.is_file():
    print("ERROR: daily_suggestor not found.")
    print(f"  Expected: {_RUN_DAILY}")
    print("  Clone your daily_suggestor repo, e.g.:")
    print(f"    git clone <repo-url> {_DEFAULT_DS}")
    print("  Or set DAILY_SUGGESTOR_DIR to an existing checkout.")
    sys.exit(1)

if str(_DAILY_SUGGESTOR) not in sys.path:
    sys.path.insert(0, str(_DAILY_SUGGESTOR))

import pandas as pd
from tqdm import tqdm

# Local scanner — only swing_mr survives post-backtest
from mean_reversion_scanner import analyse_mean_reversion

# daily_suggestor imports (resolved via sys.path injection above)
import run_daily as _ds_run_daily
from config import get_invest_for_strategy, get_ntfy_topic
from data_io import get_universe, load_ticker
from send_notification import send_notification

TOP_N_MR = 5
MR_MIN_BARS = 220  # 200 EMA + lookback


def run_swing_mr_from_local() -> list[dict]:
    """Scan swing_mr against the local CSVs that run_daily just refreshed.
    No yfinance — the CSVs already contain today's bar after step 1."""
    universe = get_universe()
    print(f"\n[swing_mr] scanning {len(universe)} tickers from local CSVs (refreshed)...")
    results = []
    for ticker in tqdm(universe, desc="swing_mr"):
        df = load_ticker(ticker)
        if df is None or len(df) < MR_MIN_BARS:
            continue
        try:
            sig = analyse_mean_reversion(ticker, df)
        except Exception:
            continue
        if sig:
            results.append(sig)
    results.sort(key=lambda x: x["volume_ratio"], reverse=True)
    top = results[:TOP_N_MR]
    if top:
        print(f"[swing_mr] {len(results)} raw, top {len(top)}:")
        for s in top:
            print(f"   {s['symbol']:<14}  CMP Rs{s['cmp']}  T Rs{s['target']}  SL Rs{s['stop_loss']}  vol {s['volume_ratio']:.1f}x")
    else:
        print("[swing_mr] no signals.")
    return top


def ensure_invested_rs_column():
    """Additive-only schema migration: add invested_rs to mean_reversion_signals."""
    from db import get_connection, SCHEMA
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    ALTER TABLE IF EXISTS {SCHEMA}.mean_reversion_signals
                    ADD COLUMN IF NOT EXISTS invested_rs NUMERIC(10,2)
                """)
    finally:
        conn.close()


def save_swing_mr(signals: list[dict]):
    if not signals:
        return
    from db import ensure_table, delete_today_signals, save_signals, get_connection, SCHEMA
    from performance import log_new_signals

    ensure_table("mean_reversion_signals")
    ensure_invested_rs_column()
    delete_today_signals("mean_reversion_signals")
    save_signals(signals, "mean_reversion_signals")

    invest = get_invest_for_strategy("swing_mr")
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    UPDATE {SCHEMA}.mean_reversion_signals
                    SET invested_rs = %s
                    WHERE date = CURRENT_DATE AND invested_rs IS NULL
                """, (invest,))
    finally:
        conn.close()

    log_new_signals(signals, "mean_reversion")
    print(f"[swing_mr] saved {len(signals)} signals @ Rs{invest:,.0f}/trade")


def send_summary_ntfy(mr_signals: list[dict]):
    """Minimal: only swing_mr buy rows — ticker | buy range | target | SL | strategy."""
    topic = get_ntfy_topic()
    if not topic:
        print("[summary] ntfy_topic.txt missing — skip")
        return
    if not mr_signals:
        return  # nothing extra to push; run_daily already sent ML buys
    lines = [f"BUYS {pd.Timestamp.today().date()}"]
    for s in mr_signals:
        lines.append(
            f"{s['symbol']} | {s['entry_min']}-{s['entry_max']} | "
            f"T {s['target']} | SL {s['stop_loss']} | MiRana"
        )
    ok = send_notification(topic=topic, message="\n".join(lines), title="BUYS")
    print(f"[summary] ntfy {'OK' if ok else 'FAILED'}")


def main():
    print("=" * 80)
    print("DAILY ORCHESTRATOR — 7 winning strategies")
    print("=" * 80)

    # 1) Refresh + 6 ML strategies + ntfy buys/sells (run_daily does it all)
    print("\n[1/3] run_daily (refresh + 6 ML strategies + ntfy) ...")
    try:
        _ds_run_daily.main()
    except SystemExit:
        pass
    except Exception as e:
        print(f"[orchestrator] run_daily FAILED: {e}")

    # 2) swing_mr against local CSVs that step 1 just refreshed
    print("\n[2/3] swing_mr (local CSVs, no second yfinance hit) ...")
    mr_signals = []
    try:
        mr_signals = run_swing_mr_from_local()
        save_swing_mr(mr_signals)
    except Exception as e:
        print(f"[orchestrator] swing_mr FAILED: {e}")

    # 3) Summary ntfy
    print("\n[3/3] summary ntfy ...")
    try:
        send_summary_ntfy(mr_signals)
    except Exception as e:
        print(f"[orchestrator] summary ntfy FAILED: {e}")

    print("\n[orchestrator] done.")


if __name__ == "__main__":
    sys.argv = [sys.argv[0]]  # ensure run_daily.main()'s argparse sees no flags
    main()
