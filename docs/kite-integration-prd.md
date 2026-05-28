# PRD: Kite Connect Integration & Trading Dashboard
**Project**: MCube Stocks — mcubetechstudio.com/stocks  
**Authors**: Sanjay + Brother  
**Date**: 2026-05-28  
**Status**: Draft

---

## 1. Context & Scope

The existing app at `mcubetechstudio.com/stocks` already has:
- Username/password auth (better-auth) with a `section: "stocks"` gate
- Scanner-generated signals (breakout, EMA pullback, VCP, etc.)
- A basic portfolio page
- Push notifications infrastructure

This PRD covers everything needed to go from "signal viewer" to "connected trading dashboard" — Kite OAuth, one-click order placement, portfolio synced from Kite, and a path to full automation.

**What is NOT changing:**
- The `/stocks` route stays. No subdomain.
- Existing better-auth (username/password) stays as the app login.
- Your brother's Python scanner stays untouched — he owns signal generation.
- Next.js API routes (your side) handle all Kite operations.

---

## 2. Users

| User | Role | Kite Account |
|------|------|--------------|
| Sanjay | You | Personal Kite account |
| Brother | ML/Backend | His own Kite account |

Two users, each logs into the app with their credentials, each connects their own Kite account via OAuth. One shared Kite Connect app (one `api_key` / `api_secret` pair in env vars). Each user gets their own `access_token` after OAuth.

---

## 3. Feature Breakdown

### 3.1 Kite Connect Setup (One-time)

**What needs to happen before any dev:**
1. One of you creates a Kite Connect app at `kite.trade/developers`
2. Set the redirect URL to `https://mcubetechstudio.com/api/kite/callback`
3. Add `KITE_API_KEY` and `KITE_API_SECRET` to Vercel env vars
4. Plan: Kite Connect **Personal plan is free** (as of March 2025) — covers orders, portfolio, holdings, GTT. Only upgrade to Connect (₹500/month) if you want live WebSocket price streaming via Kite.

---

### 3.2 Auth: No Changes + Small Extension

The existing two user accounts stay exactly as-is. The only addition is storing each user's Kite `access_token` (and its expiry) against their user ID.

**DB addition — Neon Postgres:**
```sql
CREATE TABLE kite_sessions (
  user_id       TEXT PRIMARY KEY,   -- matches better-auth user id
  access_token  TEXT NOT NULL,
  token_date    DATE NOT NULL,       -- date token was issued (expires next day 6am)
  connected_at  TIMESTAMPTZ DEFAULT now()
);
```

No new login page. No new auth flow. Just a "Connect Kite" button in Settings.

---

### 3.3 Kite OAuth Flow (Connect / Reconnect)

**Every day, once:**

```
User clicks "Connect Kite" in Settings
  → GET /api/kite/login
    → Redirect to: https://kite.trade/connect/login?api_key=XXX&v=3
      → User logs into Kite
        → Kite redirects to /api/kite/callback?request_token=YYY&status=success
          → Server: exchange request_token for access_token
            → Store in kite_sessions for this user
              → Redirect to /stocks (or /stocks/portfolio)
```

**API routes:**
| Route | Method | Purpose |
|-------|--------|---------|
| `/api/kite/login` | GET | Generate Kite login URL, redirect user |
| `/api/kite/callback` | GET | Receive `request_token`, exchange for `access_token`, store it, redirect |
| `/api/kite/status` | GET | Is this user's token valid? Returns `{ connected: bool, expired: bool }` |
| `/api/kite/disconnect` | POST | Delete user's token from DB |

**Token expiry rule:** Kite tokens expire at ~6:00 AM IST daily. A token is considered expired if `token_date < today` (IST). Check this on every Kite API call.

---

### 3.4 Daily Reconnect Notification (8:30 AM IST)

You already have push notifications wired up. Add a Vercel Cron job:

**`vercel.json` cron:**
```json
{
  "crons": [
    { "path": "/api/cron/kite-remind", "schedule": "0 3 * * 1-5" }
  ]
}
```
`0 3 * * 1-5` = 3:00 AM UTC = 8:30 AM IST, weekdays only.

**`/api/cron/kite-remind` logic:**
1. Fetch all users with push subscriptions
2. Check `kite_sessions` — is today's token missing for any of them?
3. If expired/missing → send push: *"Market opens in 30 min. Tap to reconnect your Kite account."*
4. Deep-link the notification to `/stocks/settings`

**Settings page UX:**
- Green pill: "Kite connected · refreshed today"
- Yellow pill + button: "Token expired — Reconnect Kite"
- This banner also appears at top of the main `/stocks` page if token is missing

---

### 3.5 One-Click Trading (Phase 1 — Ship First)

This is the core Phase 1 feature. Every signal card gets a **Buy** button. You click it, the order goes to Kite. No confirmation dialog for speed; but there's a cancel window.

#### Order placement flow:
```
User clicks [Buy] on a signal card
  → POST /api/kite/order
    Body: { symbol, exchange, qty, orderType: "MARKET" | "LIMIT", price? }
  → Server calls KiteConnect.placeOrder(...)
  → Returns { orderId, status }
  → UI shows: green toast "Order placed · ₹XXXX · [Cancel]" (5 sec window)
    → Cancel within 5s → POST /api/kite/order/:id/cancel
    → After 5s → toast closes, order is live
```

#### Signal card additions:
- `[Buy]` button — places a MARKET BUY at current price
- `[Sell]` button — visible only if you hold that stock (cross-reference holdings)
- Quantity input (pre-fill with a default from Settings: e.g. "₹10,000 per trade")
- Small badge on card if you already hold the stock

#### New DB table — `trades` (Neon Postgres):
```sql
CREATE TABLE trades (
  id           SERIAL PRIMARY KEY,
  user_id      TEXT NOT NULL,
  signal_id    INTEGER,             -- FK to signal that triggered the trade
  symbol       TEXT NOT NULL,
  exchange     TEXT NOT NULL,       -- NSE / BSE
  kite_order_id TEXT,
  order_type   TEXT NOT NULL,       -- BUY / SELL
  qty          INTEGER NOT NULL,
  price        NUMERIC(10,2),       -- actual executed price (filled async)
  target_price NUMERIC(10,2),
  stop_loss    NUMERIC(10,2),
  status       TEXT DEFAULT 'OPEN', -- OPEN / CLOSED / CANCELLED
  strategy     TEXT,                -- which scanner strategy (ema-pullback, vcp, etc.)
  created_at   TIMESTAMPTZ DEFAULT now(),
  closed_at    TIMESTAMPTZ
);
```

#### New API routes:
| Route | Method | Purpose |
|-------|--------|---------|
| `/api/kite/order` | POST | Place a new order |
| `/api/kite/order/:id/cancel` | POST | Cancel within 5s window |
| `/api/kite/orders` | GET | List today's orders from Kite |

---

### 3.6 Portfolio Page (Revamp)

The existing `/stocks/portfolio` page becomes a real-time trading dashboard pulling live data from Kite.

#### Tabs / sections:

**Holdings tab** (long-term positions):
- Pulls from `GET /api/kite/holdings` → KiteConnect.getHoldings()
- Columns: Stock · Qty · Avg Buy · LTP · Day P&L · Total P&L% · Target · SL
- Color coded: green if above target, red if below SL
- "Sell" button per row (pre-filled with current qty)

**Positions tab** (today's intraday):
- Pulls from `GET /api/kite/positions` → KiteConnect.getPositions()
- Same columns + realised/unrealised split

**My Trades tab** (your internal log):
- Reads from your `trades` table
- Shows which strategy signal led to each trade
- P&L calculated from avg buy price vs current price
- Filter by: strategy, date range, open/closed

**Set Target & SL:**
- Each row in Holdings/My Trades has "Set Target / SL" icon
- Opens a small inline form to set `target_price` and `stop_loss` in your `trades` table
- These are tracked in your DB, not sent to Kite (for now)

#### New API routes:
| Route | Method | Purpose |
|-------|--------|---------|
| `/api/kite/holdings` | GET | Proxy Kite holdings for this user |
| `/api/kite/positions` | GET | Proxy Kite positions for this user |
| `/api/trades` | GET | Your internal trades log |
| `/api/trades/:id` | PATCH | Update target/SL on a trade |

---

### 3.7 Phase 2 — Fully Automated Trading (Later)

Once you've tested one-click for a few weeks and trust the signals:

1. Add a toggle in Settings: "Auto-execute signals" (OFF by default)
2. Scanner (Python) writes signals to DB as usual
3. Add a Vercel Cron (or polling endpoint) that runs every 5–10 min during market hours (9:15–15:30 IST)
4. Cron checks for new signals since last run → places orders automatically for users with auto-execute ON
5. Slack/push notification sent for each auto-placed order
6. Emergency kill switch: "Pause all automation" button

**The interface you're building in Phase 1 works identically in Phase 2** — the only change is the trigger (human click vs. cron).

---

## 4. Technical Architecture

```
Browser (Next.js)
  └── /stocks/portfolio          ← revamped portfolio dashboard
  └── /stocks/settings           ← Kite connect/disconnect + token status
  └── Signal cards (existing)    ← + Buy/Sell buttons

Next.js API Routes
  ├── /api/kite/login            ← redirect to Kite OAuth
  ├── /api/kite/callback         ← receive token, store in DB
  ├── /api/kite/status           ← is user connected?
  ├── /api/kite/order            ← place / cancel order
  ├── /api/kite/holdings         ← proxy Kite holdings
  ├── /api/kite/positions        ← proxy Kite positions
  ├── /api/kite/orders           ← proxy Kite orders
  ├── /api/trades                ← internal trade log CRUD
  └── /api/cron/kite-remind      ← daily 8:30am push notification

External
  └── KiteConnect SDK (kiteconnect npm)  ← wraps all Kite REST calls
  └── Kite servers (OAuth + order API)

DB
  ├── Neon Postgres
  │   ├── kite_sessions          ← per-user access tokens
  │   ├── trades                 ← internal trade log + target/SL
  │   └── swing.*                ← existing signal tables (unchanged)
  └── MongoDB Atlas
      └── better-auth users      ← unchanged
```

---

## 5. New Pages & UI Changes Summary

| What | Where | Change Type |
|------|-------|-------------|
| Kite connect/disconnect | `/stocks/settings` | Add section to existing page |
| Token expired banner | `/stocks` (main page) + layout | New conditional banner |
| Buy / Sell buttons | All signal cards | Add to existing `signal-card.tsx` |
| Portfolio revamp | `/stocks/portfolio` | Full revamp with Kite data |
| Trade log tab | `/stocks/portfolio` | New tab |

---

## 6. Implementation Order

### Sprint 1 — Foundation (do this first, everything else depends on it)
1. Install `kiteconnect` npm package
2. Create `kite_sessions` table in Neon
3. Build `/api/kite/login` + `/api/kite/callback`
4. Add "Connect Kite" section to Settings page
5. Build `/api/kite/status` + show token status in Settings

### Sprint 2 — Trading
6. Create `trades` table in Neon
7. Build `/api/kite/order` (place + cancel)
8. Add Buy/Sell buttons to signal cards
9. Add toast with 5-second cancel window

### Sprint 3 — Portfolio
10. Build `/api/kite/holdings` + `/api/kite/positions`
11. Revamp `/stocks/portfolio` with Holdings + Positions tabs
12. Build `/api/trades` CRUD
13. Add My Trades tab with target/SL editor

### Sprint 4 — Notifications & Polish
14. Build `/api/cron/kite-remind`
15. Add `vercel.json` cron schedule
16. Add token-expired banner on main stocks page
17. Add default trade size to Settings (₹ per trade)

### Sprint 5 — Phase 2 (later, after real-world testing)
18. Auto-execute toggle in Settings
19. Cron-based signal execution
20. Kill switch

---

## 7. Open Questions / Decisions Needed

| # | Question | Notes |
|---|----------|-------|
| 1 | Kite Connect plan | Personal plan is **free** — covers orders + portfolio. ✅ No cost. |
| 2 | Redirect URL | Must set `https://mcubetechstudio.com/api/kite/callback` in Kite developer console |
| 3 | Exchange default | NSE for all signals, or configurable per stock? |
| 4 | Default order type | MARKET order for simplicity, or LIMIT at signal price? |
| 5 | Default qty / position sizing | Fixed ₹ amount per trade, or fixed qty, or % of capital? |
| 6 | Whose Kite app | One of you creates the developer app — same app, both of your Kite accounts authenticate through it |

---

*End of PRD*
