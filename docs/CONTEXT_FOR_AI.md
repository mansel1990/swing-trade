# AI Context — Swing Trading System (as of 2026-05-14)

Feed this file + `C:\Projects\mcube\docs\CONTEXT_FOR_AI.md` to Claude at the start of a new session to resume work with full context.

---

## What this system is
A personal swing trading assistant for NSE (Indian) stocks. Two parts:
1. **Python scanner** (`C:\Projects\trading\scanner\`) — runs 5 strategies each evening, saves top-5 signals per strategy to Neon Postgres
2. **Next.js dashboard** (`C:\Projects\mcube\`) — displays signals, tracks performance, shows strategy explanations

## Current state (2026-05-14)
- All 5 strategies implemented, deployed, working in production
- 5 signals each for EMA Pullback + RS Resilience saved today (Nifty is currently weak)
- Performance page shows all 5 strategies with colored badges
- Sidebar first-click delay fixed (removed duplicate auth checks)
- Top 5 per strategy limit enforced (was 10, changed to 5 for discipline)
- Info drawer on every strategy page explaining all filters in plain English

## Python scanner — key facts
- **Location:** `C:\Projects\trading\scanner\`
- **GitHub:** https://github.com/mansel1990/swing-trade
- **Deployment:** Railway, cron `30 12 * * 1-5` (12:30 UTC = 6 PM IST weekdays)
- **Root dir in Railway:** `scanner/` (important — must be set in Railway settings)
- **Run command:** `python main.py --save`
- **Test command (no DB write):** `python main.py`
- **Single strategy:** `python main.py --strategy ema` (ema | breakout | vcp | rs | mr)

## Strategies (5 total)
| Key | Module | Table | Regime |
|---|---|---|---|
| breakout | main.py (inline) | swing.signals | Bull / trending |
| ema | ema_scanner.py | swing.ema_signals | Bull / trending |
| vcp | vcp_scanner.py | swing.vcp_signals | Any (rare) |
| rs | rs_scanner.py | swing.rs_signals | Nifty weak only |
| mr | mean_reversion_scanner.py | swing.mean_reversion_signals | Ranging / correcting |

**TOP_N = 5** — each strategy saves its top 5 by volume_ratio

## DB tables (Neon, swing schema)
- swing.signals
- swing.ema_signals
- swing.vcp_signals
- swing.rs_signals
- swing.mean_reversion_signals
- swing.strategy_performance (performance tracker — all strategies)

There is also a `stocks` schema used for daily OHLCV charts — do NOT touch it.

## Next.js app — key facts
- **Location:** `C:\Projects\mcube\`
- **Deployment:** Vercel (auto-deploy on push to main)
- **Stocks section path prefix:** `/stocks/`
- **Auth:** better-auth, 1-year session, MongoDB Atlas
- **DB for signals:** Neon Postgres via `lib/sql.ts`

## mcube stocks pages
| URL | File | Color |
|---|---|---|
| /stocks | app/(stocks)/stocks/page.tsx | blue |
| /stocks/breakout | app/(stocks)/stocks/breakout/ | violet |
| /stocks/ema-pullback | app/(stocks)/stocks/ema-pullback/ | emerald |
| /stocks/vcp | app/(stocks)/stocks/vcp/ | purple |
| /stocks/rs-resilience | app/(stocks)/stocks/rs-resilience/ | rose |
| /stocks/mean-reversion | app/(stocks)/stocks/mean-reversion/ | teal |
| /stocks/performance | app/(stocks)/stocks/performance/ | amber |
| /stocks/chart | app/(stocks)/stocks/chart/ | slate |

## Critical files to read when resuming
- `C:\Projects\trading\scanner\main.py` — orchestrator
- `C:\Projects\trading\scanner\indicators.py` — all indicator helpers
- `C:\Projects\mcube\components\app-shell.tsx` — NAV_CONFIG, mobile nav, TAB_COLORS
- `C:\Projects\mcube\components\stocks\swing\signal-card.tsx` — shared signal card
- `C:\Projects\mcube\components\stocks\swing\strategy-info-drawer.tsx` — info drawer
- `C:\Projects\mcube\app\(stocks)\stocks\performance\performance-client.tsx` — STRATEGY_META

## Common tasks and where to look
| Task | Files |
|---|---|
| Add new NSE stock | `scanner/stocks.py` |
| Tune strategy parameters | top of each `*_scanner.py` |
| Add a 6th strategy | See `docs/adding-a-strategy.md` |
| Add a new mcube page | Clone `app/(stocks)/stocks/vcp/` folder |
| Update mobile nav | `components/app-shell.tsx` — NAV_CONFIG.stocks + STRATEGY_HREFS |
| Add info drawer for new strategy | `components/stocks/swing/strategy-info-drawer.tsx` — STRATEGY_INFO record |
| Change performance colors | `performance-client.tsx` — STRATEGY_META |
| Check DB tables | Neon console → swing schema |
| Check Railway cron logs | Railway dashboard → Deployments |

## Known issues / decisions
- RS Resilience only produces signals when Nifty is weak — zero signals on strong days is correct behavior
- VCP signals are rare (0–5/day across 180 stocks) — that's expected
- `breakout_level` column stores different things per strategy (resistance, EMA, pivot, support) — the UI shows whatever `levelLabel` the page passes to SignalCard
- Session is 1 year — users don't need to re-login
- Recharts Tooltip formatter must accept `number | undefined` — use `v ?? 0` to avoid TypeScript errors
