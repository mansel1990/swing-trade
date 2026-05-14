# How to Add a New Strategy

Follow this exact pattern. The existing 5 strategies are the template.

## Step 1 — Add any new indicator helpers to `indicators.py`
Only pure math goes here. No strategy logic. Return floats or pd.Series.
```python
def my_new_indicator(series: pd.Series, period: int) -> float:
    ...
```

## Step 2 — Create `scanner/<name>_scanner.py`
```python
"""
One-line strategy description.

Filters (all must pass):
  1. ...
  2. ...
"""

import sys
import pandas as pd
from indicators import calculate_rsi, calculate_volume_ratio, calculate_ema
# import any new helpers

# Parameters
RSI_MIN = 45
...
MIN_ROWS_NEEDED = 210   # must have enough history for your EMAs

def analyse_<name>(symbol: str, df: pd.DataFrame) -> dict | None:
    try:
        close  = df["Close"].dropna()
        high   = df["High"].dropna()
        low    = df["Low"].dropna()
        volume = df["Volume"].dropna()

        if len(close) < MIN_ROWS_NEEDED:
            return None

        # ... all filters ...

        return {
            "symbol":          symbol.replace(".NS", ""),
            "company_name":    symbol.replace(".NS", ""),
            "cmp":             round(float(close.iloc[-1]), 2),
            "breakout_level":  <your reference level>,
            "entry_min":       <entry lower>,
            "entry_max":       <entry upper>,
            "target":          <target price>,
            "stop_loss":       <stop price>,
            "volume_ratio":    calculate_volume_ratio(volume, 20),
            "rsi":             calculate_rsi(close),
            "signal_strength": "Strong" if <condition> else "Moderate",
        }

    except Exception as e:
        print(f"  [{symbol}] <name> error: {e}", file=sys.stderr)
        return None
```

## Step 3 — Wire into `main.py`

### Import
```python
from <name>_scanner import analyse_<name>
```

### Add flag
```python
run_<name> = "<key>" in selected
```

### Call in the per-symbol loop
```python
x_signal = analyse_<name>(sym, df) if run_<name> else None
if x_signal:
    <name>_results.append(x_signal)
    tag.append("<LABEL>")
```

### Sort, trim, print, save
Follow the exact pattern in the existing `save_jobs` list:
```python
save_jobs = [
    ...
    (run_<name>, "<name>_signals", top_<name>, "<log_strategy_name>"),
]
```

## Step 4 — DB table is created automatically
`ensure_table("<name>_signals")` runs before `save_signals()`. No SQL needed.

## Step 5 — mcube frontend

### API route
Create `app/api/stocks/swing/<name>/route.ts`:
```typescript
import { NextResponse } from "next/server";
import { sql } from "@/lib/sql";
export async function GET() {
  const rows = await sql`SELECT * FROM swing.<name>_signals WHERE date = CURRENT_DATE ORDER BY volume_ratio DESC`;
  return NextResponse.json(rows);
}
```

### Page
Create `app/(stocks)/stocks/<name>/page.tsx` + `<name>-client.tsx`.
Clone from `app/(stocks)/stocks/vcp/` — change color, icon, API URL, `levelLabel`, strategy key for info drawer.

### Sidebar entry
In `components/app-shell.tsx` → `NAV_CONFIG.stocks`, add:
```typescript
{ label: "<Name>", href: "/stocks/<name>", icon: SomeIcon, exact: false, color: "<color>" }
```
Add the color to `TAB_COLORS` if it doesn't exist.

### Info drawer content
In `components/stocks/swing/strategy-info-drawer.tsx`, add a `StrategyKey` union value and a new entry to `STRATEGY_INFO`.

### Performance filter tab
In `components/stocks/swing/signal-card.tsx` → `STRATEGY_META`, add:
```typescript
<log_strategy_name>: { label: "<Name>", color: "text-<color>-700", bg: "bg-<color>-50", border: "border-<color>-200", activeBtn: "bg-<color>-600 text-white" }
```

## Step 6 — Test
```bash
python main.py --strategy <key>              # scan only
python main.py --strategy <key> --save      # scan + save
```
Check Neon console for new table. Open mcube page to verify UI.
