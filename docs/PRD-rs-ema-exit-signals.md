# PRD: Two-Strategy Focus (RS + EMA) + EOD Exit Signals

**Project:** MCube Stocks — scanner (`C:\Projects\trading`) + frontend (`C:\Projects\mcube`)
**Author:** Sanjay
**Date:** 2026-06-08
**Build target:** implementable by Claude Sonnet without prior conversation context
**Status:** Ready to build

---

## 0. Context for the builder (read first)

Two repos:
- **Scanner** (`C:\Projects\trading\scanner`, Python): runs every evening via cron (`main.py --save`), scans ~150 NSE stocks, writes signals to Neon Postgres (schema `swing`), and tracks a **paper portfolio** in `swing.strategy_performance` (₹10,000 simulated per trade).
- **Frontend** (`C:\Projects\mcube`, Next.js + Neon): reads those tables, shows signal cards, has Kite (Zerodha) buy/sell wiring, a `current-price` API, push notifications, and a performance page.

**Workflow:** scanner auto-runs nightly and auto-logs every signal as an open paper position. The user reviews signals in the app and **manually** clicks Buy/Sell (optionally routed to Kite). Buying is never automated.

**Goal of this PRD:** (1) cut from 7 strategies to 2 (`rs_resilience`, `ema_pullback`), (2) replace the fixed +6% profit target with **evening (EOD) exit signals** based on trend break, (3) make exit signals show up on the stocks the user actually holds, (4) reduce noise so capital concentrates in genuine leaders, (5) preserve all historical paper-trade data.

**Why** (evidence): over 20 May–8 Jun 2026, Nifty fell ~2% yet `rs_resilience` returned +₹3,026 (51% win) and `ema_pullback` +₹1,155 (41% win); all other strategies were negative. RS is real relative-strength alpha in weak markets; EMA carries trends. The fixed +6% target capped every winner (HINDALCO hit +6% and was re-bought 3× instead of ridden) and 84% of RS profit came from 2 names due to daily re-entry.

---

## 1. Scope

**In scope**
1. Remove 5 strategies (code), retain their historical data.
2. New EOD exit-signal engine in the scanner (no fixed target).
3. One-open-position-per-symbol-per-strategy cap.
4. `swing.exit_signals` table + frontend surfacing on held positions.
5. Noise-reduction entry filters + rank-by-RS-strength.
6. Backtest the new exit with transaction costs before trusting it.

**Out of scope**
- Fully automated buying (stays manual).
- New broker integrations.

---

## 2. Strategies: keep two, remove five

**Keep:** `rs_resilience`, `ema_pullback`.
**Remove (code):** `breakout`, `vcp`, `mean_reversion`, `fib_pullback`, `fear_reversion`.

### 2.1 Data retention (answers "clean data or hold on?")
**Do NOT delete historical data.** Keep:
- `swing.strategy_performance` rows for all strategies (the track record + only existing RS evidence).
- The per-strategy signal tables (`swing.breakout_signals`, `swing.vcp_signals`, etc.) as read-only archive.

Removing strategy **code** does not drop tables. Leave tables in place. The only data action is in §4.3 (segregate old vs new exit logic so stats stay honest). If disk ever matters, archive old tables later — not now.

### 2.2 Backend removal checklist (`C:\Projects\trading\scanner`)
Before deleting, know these dependencies:
- `main.py` imports every scanner and has `run_<x>` flags, result lists, `print_section` calls, save blocks, and `--strategy` choices → remove the 5 everywhere.
- `fear_reversion` pulls **India VIX** (`^INDIAVIX`) → remove that fetch + `is_vix_elevated`.
- `mean_reversion` has a separate backtest engine (`backtest/mean_reversion_backtest.py`).
- Delete files: `vcp_scanner.py`, `mean_reversion_scanner.py`, `fib_pullback_scanner.py`, `fear_reversion_scanner.py`, and the breakout logic in `main.py`.
- `backtest/strategies_backtest.py` references all strategies → trim to RS + EMA.
- **Leave `db.py` table-creation for archived tables** (or no-op) so old reads don't break.

### 2.3 Frontend removal checklist (`C:\Projects\mcube`)
- `lib/stocks/types.ts`: `SignalSource` union, `SOURCE_SHORT`, `SOURCE_PRIORITY`, `SOURCE_META`, `sourceToStrategyKey` → reduce to `manish | rs_resilience | ema_pullback` (keep `manish`). Nav is derived from `SOURCE_PRIORITY`, so editing it updates the menu.
- `lib/stocks/signal-helpers.ts`: `fetchSwingSource` switch + `fetchAllSignalsBySource` → drop the 5 sources.
- `lib/stocks/signal-mappers.ts`: remove dead mappings.
- Delete API routes `app/api/stocks/swing/{breakout,vcp,mean-reversion,fib-pullback,fear-reversion}/route.ts`.
- Delete pages `app/(stocks)/stocks/{breakout,vcp,mean-reversion,fib-pullback,fear-reversion}/page.tsx`.
- Performance page: default filter to RS + EMA; keep historical rows visible under an "Archive" toggle.

---

## 3. Exit signals — the core feature

### 3.1 Concept
No fixed profit target. Each evening, for every **open** position, the scanner checks the just-closed daily bar and emits an **EXIT** signal when the trend that justified the trade breaks. The user sees the exit signal that night and manually sells.

### 3.2 Exit rules (evaluated EOD, in order; first match wins)
For an open position with `entry_price`, `stop_loss`, `days_held`, daily close series `close`, and (for RS) Nifty close series `nifty_close`:

```python
# indicators already exist: calculate_ema, mansfield_rs
TIME_BACKSTOP_DAYS = 20
PROFIT_RATCHET_PCT = 0.08

def eod_exit_signal(close, nifty_close, strategy, entry_price, stop_loss, days_held):
    px     = float(close.iloc[-1])
    ema20  = float(calculate_ema(close, 20).iloc[-1])
    ema10  = float(calculate_ema(close, 10).iloc[-1])
    unreal = (px - entry_price) / entry_price

    if px < stop_loss:                          # 1. disaster stop
        return "stop_loss", px
    trend_ref = ema10 if unreal >= PROFIT_RATCHET_PCT else ema20
    if px < trend_ref:                          # 2. trend break (primary, uncapped)
        return "trend_break", px
    if strategy == "rs_resilience" and unreal > 0:   # 3. RS fade (in profit only)
        if mansfield_rs(close, nifty_close, 20) < mansfield_rs(close.iloc[:-5], nifty_close.iloc[:-5], 20):
            return "rs_fade", px
    if days_held >= TIME_BACKSTOP_DAYS:         # 4. time backstop
        return "time_exit", px
    return None, None
```

Rules: **no target.** Winners run until rule 2/3/4. The profit ratchet tightens the trend reference from 20 EMA → 10 EMA once up ≥8% (protects big gains like HINDALCO without capping them).

### 3.3 Who is "in holding"? (answers "you must know which signals were bought")
Two holding contexts; handle both:

**A. Paper book (authoritative signal list).** Every signal the scanner emits is auto-logged to `swing.strategy_performance` with `status='open'`. This IS the list of "what was signalled and is still open." The exit engine iterates exactly these rows — so the scanner always knows its own open paper positions. No guessing.

**B. The user's real holdings (manual buys).** When the user clicks Buy, it's recorded in the `trades` table (columns include `symbol`, `strategy`, `status`) and/or appears in Kite holdings. The frontend already passes `holdingQty` into the signal card.

**Mechanism:** the exit engine emits an exit signal **per (symbol, strategy)** for every paper position it closes (§3.4). The **frontend** then matches those exit signals to the user's real book: show the red EXIT badge + Sell button on a card only when the user holds that `(symbol, strategy)` — i.e. `holdingQty > 0` or an open row in `trades`. So the paper book drives signal generation; the user's real holdings drive what gets flagged for action. A name the user never bought simply shows the exit in the paper stats but raises no Sell prompt.

### 3.4 Backend changes (`scanner/performance.py`)
- Replace `evaluate_exit_one_day` (fixed target/SL/7-day) with `eod_exit_signal` (§3.2). Fetch ~60 daily closes per open symbol (+ Nifty for RS) instead of just today's OHLC, so EMAs/RS can be computed.
- When a position closes on an exit rule: call `_close_position(...)` **and** insert into `swing.exit_signals` (§3.5).
- Change `MAX_HOLD_DAYS` 7 → `TIME_BACKSTOP_DAYS` 20.

### 3.5 New table (`scanner/db.py`)
```sql
CREATE TABLE IF NOT EXISTS swing.exit_signals (
  id          SERIAL PRIMARY KEY,
  date        DATE NOT NULL,
  symbol      VARCHAR(20) NOT NULL,
  strategy    VARCHAR(30) NOT NULL,
  reason      VARCHAR(20) NOT NULL,   -- trend_break | stop_loss | rs_fade | time_exit
  ref_price   NUMERIC(10,2),
  created_at  TIMESTAMP DEFAULT NOW(),
  UNIQUE (date, symbol, strategy)
);
```

### 3.6 One-position-per-symbol cap (`scanner/performance.py:log_new_signals`)
Skip logging a new paper position if an open one already exists for that symbol+strategy:
```sql
SELECT 1 FROM swing.strategy_performance
WHERE symbol = %s AND strategy = %s AND status = 'open'
```
The scanner still *displays* the signal (so the user sees the name is still strong) but passes a `held=True` flag for the UI to grey it (§5.3). This removes the daily re-entry pyramiding that caused 84% concentration.

---

## 4. Noise reduction — concentrate in real leaders

### 4.1 The honest answer (re "identify the 2 winners, remove noise")
You **cannot** know in advance which 2 names will be the monster winners — picking HINDALCO/SAIL ahead of time is hindsight. What you *can* do is structure the system so capital concentrates into winners statistically:

1. **Let the exit do the pruning.** With the trend-break exit, noise names break their 20 EMA within days and exit at a small loss; genuine leaders hold their EMA and you ride them. This is how momentum portfolios self-concentrate — enter a basket, cut losers fast, let winners run. No prediction required.
2. **Rank by RS strength, not volume.** Today signals are sorted by `volume_ratio` (irrelevant to RS). Change the RS sort key to **relative outperformance** = `stock_10d_return − nifty_10d_return` (and/or Mansfield RS slope). Then the top of the list is the strongest relative performers — the HINDALCO-type names — not the highest-volume ones.
3. **Add persistence + quality filters at entry** (trims weak signals before they're taken):
   - RS line (stock/nifty ratio) at/near a **20-day high** (leadership is persistent, not a 1-day blip).
   - Price above a **rising** 50 EMA (50 EMA today > 50 EMA 10 days ago).
   - Minimum liquidity: 20-day avg traded value ≥ a floor (e.g. ₹25–50 cr/day) to drop illiquid noise.
   - Optional trend strength: ADX(14) ≥ 20.
4. **Cap the book.** With one-position-per-symbol + a max open count (e.g. 8–10 across both strategies), capital naturally sits in the survivors.

### 4.2 Where to change (scanner)
- `rs_scanner.py`: add persistence/quality filters; set the result sort key to outperformance.
- `main.py`: change `r.sort(key=lambda x: x["volume_ratio"])` → strategy-aware (RS by outperformance, EMA can stay volume/closeness).
- Add an `outperformance` (and optionally `rs_rank`) field to the RS signal dict for display.

### 4.3 Keep old vs new stats honest
Closed trades before the cutover used the +6% target logic; new ones use trend-break. Don't blend them. Either:
- add `exit_logic_version VARCHAR` to `strategy_performance` (set 'v2' for new trades), **or**
- record a cutover date and filter performance views to `signal_date >= cutover` for "current" stats while keeping all history under "Archive."

---

## 5. Frontend changes (`C:\Projects\mcube`)

### 5.1 New API route — `app/api/stocks/swing/exit-signals/route.ts`
```ts
const rows = await sql`
  SELECT symbol, strategy, reason, ref_price, date
  FROM swing.exit_signals
  WHERE date = (SELECT MAX(date) FROM swing.exit_signals)
`;
return NextResponse.json({ exits: rows });
```

### 5.2 Exit badge + Sell on held cards — `components/stocks/swing/unified-signal-card.tsx`
For an open card, if `(symbol, strategy)` is in today's exit-signals AND the user holds it (`holdingQty > 0` or open `trades` row): render a red **"EXIT — {reason}"** banner and surface the existing `KiteTradeActions` Sell button (already built; currently shown when `holdingQty > 0`). Reuse the closed-card `exit_reason` styling.

### 5.3 "Holding — no re-entry" state
When an entry signal fires for a name already held (the §3.6 cap), grey the card and show "Holding — no re-entry" instead of Log buy / Buy. Prevents manual pyramiding.

### 5.4 Replace the "Target" cell with "Trend stop"
On open cards, the StatCell currently labelled **Target** should show **"Trend stop"** = the live 20 EMA (or 10 EMA once +8%). This turns "ride until trend breaks" into a concrete daily number the user can watch.

### 5.5 Trim nav to two strategies
Per §2.3. Keep RS + EMA pages; archive the rest. Split the `manish` (`sim.stock_suggestions`) stats into their own card on the performance page so they don't blend into RS/EMA numbers (they currently merge into one `stats` array in `app/api/stocks/swing/performance/route.ts`).

### 5.6 Exit push notification
After the evening scan writes `exit_signals`, fire one push per exit (reuse existing push infra / cron pattern): *"EXIT: SELL {symbol} ({reason})"* deep-linked to the strategy page.

---

## 6. UI design — the evening view

Two strategies, three glances, top-to-bottom:

1. **Action needed (exits):** held names with an exit signal tonight — red EXIT banner, reason, one-tap Sell, live price, unrealized P&L, days held. Sorted exit-first.
2. **Open positions:** everything else held — live price, unrealized P&L, **Trend stop** (live 20/10 EMA), days held.
3. **New entries tonight:** RS + EMA cards — entry zone, disaster stop, Trend-stop line; greyed "Holding — no re-entry" for names already in the book.

---

## 7. Data model summary (final)

- `swing.rs_signals`, `swing.ema_signals` — latest entry signals (existing; RS gains `outperformance` field).
- `swing.exit_signals` — **new**, per §3.5.
- `swing.strategy_performance` — paper book; gains `exit_logic_version` (§4.3); exit reasons now include `trend_break`, `rs_fade`, `time_exit`.
- `trades` — user's real manual trades (existing; matched to exit signals by symbol+strategy).
- Archived (read-only): `swing.{breakout,vcp,mean_reversion,fib,fear_reversion}_signals` and their historical `strategy_performance` rows.

---

## 8. Build order
1. **Scanner exit engine** (§3.2–3.6) — self-contained, testable offline.
2. **Backtest with costs** (§9) — prove the new exit beats +6% target after fees, and finally measure RS.
3. `exit_signals` table + API (§3.5, §5.1).
4. Card exit badge + Sell + held state + Trend-stop cell (§5.2–5.4).
5. Remove 5 strategies, backend + frontend (§2.2–2.3).
6. Noise-reduction filters + RS sort key (§4).
7. Exit push (§5.6).

Steps 1–2 are where the edge is won. Do not ship 3–7 until the backtest confirms the exit.

---

## 9. Acceptance criteria
- Scanner runs nightly, emits RS + EMA entry signals only; the 5 removed strategies are gone from code and nav, their historical data still readable under Archive.
- No symbol has >1 open paper position per strategy.
- An open position that closes below its 20 EMA (or 10 EMA when +8%) produces a row in `swing.exit_signals` and closes the paper position with the correct `exit_reason`.
- The app shows a red EXIT + Sell on a held card the morning after its exit fires; sends one push.
- RS signal list is ordered by relative outperformance, not volume.
- Backtest (RS + EMA, 180–250 days, transaction costs included) shows the trend-break exit ≥ the old +6% target on net P&L and expectancy; RS is measured (Nifty CSV present), not skipped.
- All pre-cutover paper trades remain queryable; "current" stats use only post-cutover (v2) trades.

---

## 10. Open questions for the builder
1. Max concurrent open positions across both strategies? (suggest 8–10.)
2. Liquidity floor for the quality filter? (suggest ₹25–50 cr/day avg traded value.)
3. Cutover approach for honest stats: add `exit_logic_version` column, or filter by date? (suggest the column.)
4. Push: one notification per exit, or one digest listing all exits? (suggest digest if >3.)
