# RS + EMA — Final Two-Strategy Plan & Exit Redesign

**Date:** 2026-06-08
**Decision owner:** Sanjay
**Status:** Spec — ready to implement

---

## 1. The verdict (why these two, from real data)

Pulled `swing.strategy_performance` (last ~3 weeks, ₹10k/trade, closed trades only):

| strategy | closed | win% | avg % | median % | total ₹ |
|---|---|---|---|---|---|
| **rs_resilience** | 35 | 51.4% | +0.86 | +0.01 | **+3,026** |
| **ema_pullback** | 54 | 40.7% | +0.21 | −2.00 | +1,155 |
| fib_pullback | 10 | 20% | −1.91 | −3.00 | −1,908 |
| breakout | 2 | 0% | −1.91 | — | −381 |
| vcp | 1 | 0 closed | — | — | 0 |

Nifty over the same window: **23,659 (20 May) → ~23,189 (8 Jun) = −2.0%.**

RS made money *while the index fell 2%* → that is genuine **relative-strength alpha**, doing exactly what it was designed to do (find names that hold up in a weak tape). The regime gate firing daily was correct — the market really was weak.

**Final call: run `rs_resilience` + `ema_pullback`. Drop the rest.** RS earns alpha in weak/falling markets; EMA carries trending/bull periods. Complementary, and the only two with positive realized P&L and real samples. `fib_pullback`, `breakout`, `vcp`, `fear_reversion`, `mean_reversion` are switched off.

### Caveats we are explicitly fixing below
1. **Concentration:** HINDALCO + SAIL = 84% of RS profit. Caused by re-entering the same name daily (HINDALCO logged 6×). → fixed by *one position per symbol*.
2. **Capped winners:** every RS win is exactly +6.00% (the fixed target). HINDALCO hit +6% and was closed+rebought 3× instead of riding one move. Losses ran to −6%. → fixed by *EOD trend-break exit, no target*.
3. **Realized P&L is optimistic:** 21 RS positions are still open (mostly 2–8 Jun entries, the leg now hitting stops) and aren't in the +3,026. True mark-to-market is lower.

---

## 2. Exit strategy — answering "how do I not cap it, what's the exit?"

**Stop using a fixed +6% target.** A fixed target caps every winner at +6% while letting losers run to the stop — mathematically that bleeds the edge even at a 50%+ win rate (your median trade is ~0%).

Instead, the **evening scan emits an EXIT signal** when the trend that justified the entry breaks. You hold a winner as long as the trend holds, and you get an explicit "SELL" flag on the evening run (matches your manual click-to-sell workflow).

### The exit rules (evaluated EOD on the just-closed daily bar)

For each **open** position, exit if **any** of these fire:

1. **Trend break (primary):** daily close **< 20 EMA**. This is the uncapped exit — ride until the trend actually breaks.
2. **Protective stop (disaster):** daily close **< stop_loss** (original swing-low / 20EMA−3%). Hard floor for gap-downs.
3. **RS fade (RS strategy only):** Mansfield RS today **< Mansfield RS 5 days ago** *and* position is in profit. The relative-strength reason for the trade is gone even if price hasn't broken the EMA — take the gain.
4. **Profit ratchet (optional, protects big winners):** once unrealized ≥ **+8%**, tighten the trend reference from the 20 EMA to the **10 EMA**. Gives back less of a large gain (this is what would have kept you in HINDALCO's full run while still protecting it).
5. **Time backstop:** held **≥ 20 trading days** → exit at close. *Decision: 20 days, up from 7.* Rarely the binding exit; just stops positions being forgotten.

**No fixed profit target.** Winners run until rule 1/3/4 ends them.

### Why this fixes your two biggest problems
- **Uncaps upside:** under close-below-EMA, HINDALCO is entered **once** and held through the whole up-move, exiting only when it finally closes below the 20 EMA — one position, full trend, no churn.
- **Kills concentration churn:** because you no longer take profit at +6% and re-buy, the daily re-entries that produced the 84% concentration disappear on their own.

### Decisions locked (you delegated these)
- **Position cap:** **one open position per symbol per strategy.** No pyramiding.
- **Max hold:** **20 trading days** backstop.

---

## 3. Backend changes (Python — `C:\Projects\trading\scanner`)

All three strategies already write to `swing.strategy_performance` via `performance.py`. The exit logic lives there.

### 3.1 New exit evaluation — `performance.py`

Replace the fixed target/SL/7-day logic in `evaluate_open_positions()` with an indicator-based check. Today `evaluate_exit_one_day()` only knows `high/low/close` + fixed target/stop. The new version needs the position's **recent price series** to compute the 20/10 EMA and Mansfield RS, so fetch a short history (≈60d) per open symbol instead of just today's bar.

New core function (drop-in):

```python
# performance.py  — new EOD exit signal
from indicators import calculate_ema, mansfield_rs

TIME_BACKSTOP_DAYS = 20          # was MAX_HOLD_DAYS = 7
PROFIT_RATCHET_PCT = 0.08        # tighten to 10 EMA once +8%

def eod_exit_signal(close, nifty_close, strategy, entry_price, stop_loss, days_held):
    """
    Returns (exit_reason, exit_price) or (None, None).
    `close` = daily close series up to and including the just-closed bar.
    """
    px        = float(close.iloc[-1])
    ema20     = float(calculate_ema(close, 20).iloc[-1])
    ema10     = float(calculate_ema(close, 10).iloc[-1])
    unreal    = (px - entry_price) / entry_price

    # 2. disaster stop
    if px < stop_loss:
        return "stop_loss", px
    # 4. profit ratchet: once +8%, use 10 EMA as the trend ref
    trend_ref = ema10 if unreal >= PROFIT_RATCHET_PCT else ema20
    # 1. trend break
    if px < trend_ref:
        return "trend_break", px
    # 3. RS fade (RS strategy only, only when in profit)
    if strategy == "rs_resilience" and unreal > 0:
        rs_now = mansfield_rs(close, nifty_close, period=20)
        rs_5d  = mansfield_rs(close.iloc[:-5], nifty_close.iloc[:-5], period=20)
        if rs_now < rs_5d:
            return "rs_fade", px
    # 5. time backstop
    if days_held >= TIME_BACKSTOP_DAYS:
        return "time_exit", px
    return None, None
```

`evaluate_open_positions()` then: for each open position, fetch ~60d of daily closes (+ Nifty for RS), call `eod_exit_signal(...)`, and if it returns a reason → `_close_position(...)` **and** write an exit-signal row (3.3) so the frontend can flag your real holding.

### 3.2 One-position-per-symbol cap — `performance.py:log_new_signals()`

Current dedup only blocks same-day duplicates. Change the guard to skip if an **open** position already exists for that symbol+strategy:

```python
cur.execute(f"""
    SELECT 1 FROM {SCHEMA}.{TABLE}
    WHERE symbol = %s AND strategy = %s AND status = 'open'
""", (s["symbol"], strategy))
if cur.fetchone():
    continue   # already holding this name in this strategy — no re-entry
```

The scanner can still *display* the signal (so you see it's still strong), but it won't open a second paper position. (Optionally pass a `held=True` flag through so the UI greys it — see 4.3.)

### 3.3 Surface exit signals — `db.py` (small new table)

So the frontend can badge a held position with "EXIT":

```sql
CREATE TABLE IF NOT EXISTS swing.exit_signals (
  id           SERIAL PRIMARY KEY,
  date         DATE NOT NULL,
  symbol       VARCHAR(20) NOT NULL,
  strategy     VARCHAR(30) NOT NULL,
  reason       VARCHAR(20) NOT NULL,   -- trend_break | stop_loss | rs_fade | time_exit
  ref_price    NUMERIC(10,2),
  created_at   TIMESTAMP DEFAULT NOW(),
  UNIQUE (date, symbol, strategy)
);
```

`evaluate_open_positions()` inserts one row here whenever it closes a paper position on an exit signal. Idempotent on (date, symbol, strategy).

### 3.4 Remove the fixed target from the signal dicts
`rs_scanner.py` / `ema_scanner.py` keep emitting `entry`, `stop_loss`, `cmp`, etc. The `target` field becomes informational only (or drop it). The paper sim no longer uses `target` to exit. Keep `stop_loss` — it's now the disaster stop in rule 2.

### 3.5 Backtest it before trusting it
Update `backtest/strategies_backtest.py` to use `eod_exit_signal` instead of fixed target/timeout, provide the Nifty CSV (so RS is actually measured — it currently is skipped), and **add transaction costs** (brokerage + STT + slippage ≈ 0.3–0.5% round trip). Re-run RS + EMA over 180–250 days and confirm the new exit beats the old one after costs.

---

## 4. Frontend changes (Next.js — `C:\Projects\mcube`)

Your frontend already has: per-strategy routes, `UnifiedSignalCard`, a `current-price` API, Kite buy/sell actions, push infra, and `swing.strategy_performance` driving the performance page. We add an exit-signal layer and trim to two strategies.

### 4.1 New API route — `app/api/stocks/swing/exit-signals/route.ts`
Reads the latest `swing.exit_signals`:

```ts
const rows = await sql`
  SELECT symbol, strategy, reason, ref_price, date
  FROM swing.exit_signals
  WHERE date = (SELECT MAX(date) FROM swing.exit_signals)
`;
return NextResponse.json({ exits: rows });
```

### 4.2 Exit badge + SELL on held cards — `components/stocks/swing/unified-signal-card.tsx`
The card already renders open positions with live price + unrealized P&L. Add: if this symbol+strategy appears in today's exit-signals (and you hold it), show a red **"EXIT — {reason}"** banner and surface the existing `KiteTradeActions` Sell button (already built, currently only shown when `holdingQty > 0`). So an evening exit becomes a one-tap sell. Reuse the closed-card `exit_reason` styling.

### 4.3 Mark "already held" entries
When an entry signal fires for a name you already hold (the 3.2 cap), grey the card and label "Holding — no re-entry" instead of a Log buy button. Stops you from manually pyramiding too.

### 4.4 Trim the UI to two strategies
- Navigation / `app/(stocks)/stocks/*`: keep `rs-resilience` and `ema-pullback` pages; hide `breakout`, `vcp`, `fib-pullback`, `mean-reversion`, `fear-reversion` from the nav (leave routes for archive).
- `SOURCE_META` / `fetchAllSignalsBySource`: stop fetching the dropped sources (saves DB round-trips) or keep fetching but hide.
- Performance page: default filter to RS + EMA; keep the `sim.stock_suggestions` ("manish") line **visually separate** so it doesn't blend into RS/EMA stats (it currently merges into the same `stats` array — split it into its own card).

### 4.5 Push notification on exit (reuse existing infra)
You already have `app/api/cron/...` + push. After the evening scan writes `exit_signals`, fire one push: *"EXIT signal: SELL {symbol} ({reason})"* deep-linked to the strategy page. Mirrors your existing Kite-reconnect reminder pattern.

---

## 5. UI design — the evening view

The mental model becomes **two columns / two states**, not seven strategies:

**A. New entries (tonight)** — RS and EMA cards as today, but:
- entry zone + disaster stop only (no "Target" cell — replace that StatCell with **"Trend stop: 20 EMA ₹___"** so you see where the exit currently sits).
- "Holding — no re-entry" greyed state for names already in the book.

**B. Open positions / action needed** — a section above entries:
- each held name with live price, unrealized P&L, current 20/10 EMA trend stop, days held.
- if an exit signal fired tonight → red **EXIT** banner + one-tap **Sell**.
- sort by "exit signalled" first, then by unrealized P&L.

So every evening you open the app and see, in order: *what to sell* (exit signals), *what you're holding and where its trend stop is*, *what's new to buy*. Two strategies, three glances.

A small **"Trend stop"** line on every open card (showing the live 20-EMA, or 10-EMA once +8%) is the single most useful addition — it turns the abstract "ride until trend breaks" into a concrete number you can watch each day.

---

## 6. Suggested build order
1. **Backend exit logic** (3.1–3.4) — self-contained in `trading/scanner`, testable offline.
2. **Backtest with costs** (3.5) — confirm the new exit + RS actually win before shipping.
3. `exit_signals` table + API route (3.3, 4.1).
4. Card exit badge + Sell + held-state (4.2, 4.3).
5. Trim nav to two strategies + split the "manish" stats (4.4).
6. Exit push notification (4.5).

Steps 1–2 are where the edge is won or lost. Everything after is plumbing.
