# Trading Strategies

All strategies return the same dict shape (or None) — see adding-a-strategy.md for the contract.

---

## 1. Consolidation Breakout (`scanner/main.py` → `swing.signals`)

**Strategy key for --strategy flag:** `breakout`  
**Performance log strategy name:** `"breakout"`  
**Best market regime:** Trending / Bull

### Logic
Stocks coiling in a tight range (<5% high-low) below a resistance level. When price is within 2% of the 10-day high AND volume spikes above 1.5× average AND RSI is 50–68, a breakout is imminent.

### Parameters (top of main.py)
| Param | Value | Description |
|---|---|---|
| CONSOLIDATION_DAYS | 10 | Window for detecting tight range |
| MIN_VOLUME_RATIO | 1.5 | Volume spike threshold |
| RSI_MIN / RSI_MAX | 50 / 68 | RSI band |
| NEAR_BREAKOUT_PCT | 0.02 | Price must be within 2% of resistance |
| TARGET_PCT | 0.05 | +5% from entry |
| STOP_LOSS_PCT | 0.025 | -2.5% from entry |
| ENTRY_MAX_PCT | 0.02 | Entry zone width above breakout level |

### Signal fields
- `breakout_level` → 10-day resistance high

---

## 2. EMA Pullback (`scanner/ema_scanner.py` → `swing.ema_signals`)

**Strategy key:** `ema`  
**Performance log name:** `"ema_pullback"`  
**Best regime:** Trending / Bull

### Logic
Stocks in uptrend (20 EMA > 50 EMA) that pull back to touch the 20 EMA on low volume, then bounce back above it on higher volume. Institutions "add" at the 20 EMA in uptrends.

### Parameters (top of ema_scanner.py)
| Param | Value | Description |
|---|---|---|
| EMA_FAST / EMA_SLOW | 20 / 50 | EMAs defining the uptrend |
| PULLBACK_LOOKBACK | 3 | Days to look back for EMA touch |
| PULLBACK_TOLERANCE | 1.5% | "Close enough" to the EMA |
| RSI_MIN / RSI_MAX | 40 / 62 | RSI band (pulled back but not broken) |
| MAX_ABOVE_SLOW_EMA | 15% | Must be within 15% of 50 EMA (not extended) |
| TARGET_PCT | 0.04 | +4% from entry |
| STOP_PCT | 0.02 | -2% below 20 EMA |
| ENTRY_MAX_PCT | 0.01 | Entry zone = 20 EMA + 1% |
| BOUNCE_VOL_RATIO | 1.2 | Volume required for "Strong" rating |

### Signal fields
- `breakout_level` → 20 EMA value (the support level)

---

## 3. VCP — Volatility Contraction Pattern (`scanner/vcp_scanner.py` → `swing.vcp_signals`)

**Strategy key:** `vcp`  
**Performance log name:** `"vcp"`  
**Best regime:** Any — but strongest in early bull runs

### Logic
Mark Minervini's signature setup. 2–4 progressive pullbacks in a Stage-2 uptrend, each tighter than the last (e.g. 20% → 12% → 5%), with volume drying up in the final contraction. When price is near the pivot (last swing high), supply is exhausted and a big move is imminent.

### Parameters (top of vcp_scanner.py)
| Param | Value | Description |
|---|---|---|
| EMA_FAST / EMA_MID / EMA_SLOW | 50 / 150 / 200 | Stage 2 uptrend requirement |
| PCT_OFF_HIGH_MAX | 25% | Within 25% of 52-week high |
| PCT_OFF_LOW_MIN | 25% | Up at least 25% from 52-week low |
| CONTRACTION_LOOKBACK | 50 | Days to find contractions in |
| MIN_CONTRACTIONS / MAX_CONTRACTIONS | 2 / 4 | Valid contraction count |
| PIVOT_TOLERANCE | 2% | Price must be within 2% of pivot |
| VOL_DRY_UP_RATIO | 60% | Final contraction avg vol < 60% of window avg |
| RSI_MIN / RSI_MAX | 50 / 70 | RSI band |
| TARGET_PCT | 0.08 | +8% from pivot |
| STOP_BUFFER | 0.93 | Stop = max(final_low, pivot × 0.93) |

### Signal fields
- `breakout_level` → pivot price (last swing high)

### Notes
VCPs are rare — 0–5 per day across 180 stocks is normal. Each is high conviction.

---

## 4. Relative Strength Resilience (`scanner/rs_scanner.py` → `swing.rs_signals`)

**Strategy key:** `rs`  
**Performance log name:** `"rs_resilience"`  
**Best regime:** ONLY runs when Nifty is weak (below 20 EMA, down >2% over 10 days)

### Logic
When the index is falling, most stocks fall with it. This scanner finds the exceptions — stocks making higher lows while Nifty makes lower lows. These are the next bull market leaders.

### Parameters (top of rs_scanner.py)
| Param | Value | Description |
|---|---|---|
| NIFTY_MIN_DROP | 2% | Nifty must be down ≥2% over 10 days |
| RS_OUTPERFORM_PP | 5 pp | Stock 10-day return must beat Nifty by ≥5 pp |
| STOCK_EMA | 50 | Stock must be above its own 50 EMA |
| HIGHER_LOW_LOOKBACK | 20 | Compare 10-day lows, 20 days apart |
| RSI_MIN / RSI_MAX | 45 / 65 | RSI band |
| TARGET_PCT | 0.06 | +6% |
| STOP_PCT | 0.03 | -3% below 20 EMA |
| MANSFIELD_LOOKBACK | 5 | Days to compare Mansfield RS trend |

### Special: Nifty fetch
`fetch_index("^NSEI")` is called once at the start of `main.py`. If Nifty is not weak, the RS scan is skipped entirely — saves time and avoids noise in bull markets.

### Signal fields
- `breakout_level` → stock's 20 EMA value

---

## 5. Mean Reversion Extreme Oversold (`scanner/mean_reversion_scanner.py` → `swing.mean_reversion_signals`)

**Strategy key:** `mr`  
**Performance log name:** `"mean_reversion"`  
**Best regime:** Any — best in ranging / correcting markets

### Logic
RSI < 30 (extreme oversold) + price at major support (200 EMA or 60-day swing low) + bullish reversal candle (piercing/engulfing) + above-average volume on the reversal day. Target is the 20 EMA (mean reversion magnet).

### Parameters (top of mean_reversion_scanner.py)
| Param | Value | Description |
|---|---|---|
| RSI_MAX | 30 | Must be below 30 |
| EMA_TARGET | 20 | Target EMA (mean reversion magnet) |
| EMA_LONG | 200 | Long-term trend reference |
| SUPPORT_TOLERANCE | 3% | Within 3% of 200 EMA or swing low |
| SWING_LOW_LOOKBACK | 60 | Days for swing low reference |
| TREND_BREAK_BUFFER | 0.92 | Price must be > 200 EMA × 0.92 (not in bear collapse) |
| MIN_VOLUME_RATIO | 1.0 | At least average volume on reversal |
| STOP_PCT | 2% | Below today's low |

### Signal fields
- `breakout_level` → support level that was tested (200 EMA or 60-day swing low)

---

## Common Signal Dict Shape
All strategies return this dict (or None):
```python
{
    "symbol":          str,    # e.g. "RELIANCE" (no .NS suffix)
    "company_name":    str,    # same as symbol for now
    "cmp":             float,  # current market price
    "breakout_level":  float,  # strategy-specific reference level
    "entry_min":       float,  # lower bound of entry zone
    "entry_max":       float,  # upper bound of entry zone
    "target":          float,  # profit target price
    "stop_loss":       float,  # stop loss price
    "volume_ratio":    float,  # today's vol / 20-day avg vol
    "rsi":             float,  # 14-period RSI
    "signal_strength": str,    # "Strong" | "Moderate"
}
```
