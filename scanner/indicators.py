import pandas as pd
import numpy as np


def calculate_rsi(series: pd.Series, period: int = 14) -> float:
    """Returns the most recent RSI value for a price series."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def calculate_volume_ratio(volumes: pd.Series, avg_period: int = 20) -> float:
    """Today's volume divided by the 20-day average volume (excluding today)."""
    if len(volumes) < avg_period + 1:
        return 0.0
    avg_volume = volumes.iloc[-(avg_period + 1):-1].mean()
    if avg_volume == 0:
        return 0.0
    return round(float(volumes.iloc[-1] / avg_volume), 2)


def calculate_breakout_level(highs: pd.Series, lookback: int = 10) -> float:
    """Highest high over the last `lookback` days (excluding today)."""
    return round(float(highs.iloc[-(lookback + 1):-1].max()), 2)


def is_consolidating(highs: pd.Series, lows: pd.Series, lookback: int = 10, threshold: float = 0.05) -> bool:
    """True if the high-low range over the lookback window is tight (< threshold %)."""
    window_highs = highs.iloc[-(lookback + 1):-1]
    window_lows = lows.iloc[-(lookback + 1):-1]
    range_pct = (window_highs.max() - window_lows.min()) / window_lows.min()
    return range_pct < threshold


def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    """Returns the full EMA series. Use .iloc[-1] for the latest value."""
    return series.ewm(span=period, adjust=False).mean()


def find_contractions(highs: pd.Series, lows: pd.Series, lookback: int = 50) -> list[float]:
    """
    Detect a sequence of pullback (peak->trough) depths over the lookback window.
    Returns list of contraction depths as fractions, e.g. [0.20, 0.12, 0.05].

    Algorithm: walk the window and identify alternating swing peaks and troughs
    using a simple zig-zag with a 3-day window for local extrema. Then for each
    peak->trough pair, record (peak - trough) / peak.
    """
    h = highs.iloc[-lookback:].reset_index(drop=True)
    l = lows.iloc[-lookback:].reset_index(drop=True)
    if len(h) < 10:
        return []

    pivots = []  # list of (index, price, kind) where kind is 'H' or 'L'
    window = 3
    for i in range(window, len(h) - window):
        is_peak = h.iloc[i] == h.iloc[i - window:i + window + 1].max()
        is_trough = l.iloc[i] == l.iloc[i - window:i + window + 1].min()
        if is_peak:
            pivots.append((i, float(h.iloc[i]), "H"))
        elif is_trough:
            pivots.append((i, float(l.iloc[i]), "L"))

    # Collapse consecutive same-kind pivots (keep the more extreme one)
    collapsed = []
    for p in pivots:
        if collapsed and collapsed[-1][2] == p[2]:
            if p[2] == "H" and p[1] > collapsed[-1][1]:
                collapsed[-1] = p
            elif p[2] == "L" and p[1] < collapsed[-1][1]:
                collapsed[-1] = p
        else:
            collapsed.append(p)

    # Walk peak -> trough pairs
    contractions = []
    for i in range(len(collapsed) - 1):
        a, b = collapsed[i], collapsed[i + 1]
        if a[2] == "H" and b[2] == "L":
            depth = (a[1] - b[1]) / a[1]
            if depth > 0:
                contractions.append(round(depth, 4))

    return contractions


def mansfield_rs(stock_close: pd.Series, index_close: pd.Series, period: int = 20) -> float:
    """
    Mansfield Relative Strength: ratio of stock to index, normalized.
    Returns most recent value of the smoothed ratio. Higher = stock outperforming.
    """
    aligned_idx = index_close.reindex(stock_close.index, method="ffill")
    ratio = stock_close / aligned_idx
    smoothed = ratio.ewm(span=period, adjust=False).mean()
    return float(smoothed.iloc[-1])


def is_bullish_reversal_candle(df: pd.DataFrame, idx: int = -1) -> bool:
    """
    True if today (idx) is a bullish piercing or engulfing candle:
      - Today closes higher than today's open (green)
      - Today closes higher than yesterday's close
      - Yesterday was red (close < open)
      - Today's body covers >= 50% of yesterday's body (piercing) OR fully engulfs
    """
    try:
        today_o = float(df["Open"].iloc[idx])
        today_c = float(df["Close"].iloc[idx])
        prev_o  = float(df["Open"].iloc[idx - 1])
        prev_c  = float(df["Close"].iloc[idx - 1])
    except (IndexError, KeyError):
        return False

    today_green = today_c > today_o
    prev_red    = prev_c < prev_o
    higher_close = today_c > prev_c

    if not (today_green and prev_red and higher_close):
        return False

    prev_body = abs(prev_o - prev_c)
    if prev_body == 0:
        return False
    midpoint = (prev_o + prev_c) / 2
    pierces = today_c >= midpoint  # piercing or stronger
    return pierces


def find_swing_high_low(
    highs: pd.Series,
    lows: pd.Series,
    lookback: int = 30,
    min_bars_between: int = 5,
    min_leg_pct: float = 0.03,
) -> tuple[int, int, float, float] | None:
    """
    Find the most recent valid swing leg (an upward impulse being retraced).

    Algorithm:
      1. Inspect the last `lookback` bars EXCLUDING today.
      2. swing_high = bar with max(high) in that window.
      3. swing_low  = bar with min(low) STRICTLY BEFORE swing_high in the same window.
      4. Require at least `min_bars_between` bars between low and high.
      5. Require leg size >= min_leg_pct of swing_low.

    Returns (low_idx, high_idx, low_price, high_price) where indices are absolute
    positions in the original series, or None if no valid leg exists.
    """
    if len(highs) < lookback + 1 or len(lows) < lookback + 1:
        return None

    window_high = highs.iloc[-(lookback + 1):-1]
    window_low  = lows.iloc[-(lookback + 1):-1]
    if window_high.empty or window_low.empty:
        return None

    high_pos_in_window = int(window_high.values.argmax())
    if high_pos_in_window < min_bars_between:
        return None  # no room for a swing low before the high

    pre_high_lows = window_low.iloc[:high_pos_in_window]
    low_pos_in_window = int(pre_high_lows.values.argmin())

    if high_pos_in_window - low_pos_in_window < min_bars_between:
        return None

    swing_high = float(window_high.iloc[high_pos_in_window])
    swing_low  = float(window_low.iloc[low_pos_in_window])
    if swing_low <= 0 or swing_high <= swing_low:
        return None
    if (swing_high - swing_low) / swing_low < min_leg_pct:
        return None

    # Translate window positions to absolute indices in the input series.
    base = len(highs) - (lookback + 1)
    return (base + low_pos_in_window, base + high_pos_in_window, swing_low, swing_high)


def fib_retracement_pct(swing_low: float, swing_high: float, current: float) -> float:
    """
    Retracement % from swing_high toward swing_low.
      0.0 = at swing_high
      0.5 = exact midpoint (the discount-zone threshold)
      1.0 = back at swing_low
    Values > 0.5 are in the discount zone.
    """
    if swing_high <= swing_low:
        return 0.0
    return (swing_high - current) / (swing_high - swing_low)


def has_big_red_pullback(
    opens: pd.Series,
    closes: pd.Series,
    lookback: int = 5,
    body_multiplier: float = 1.5,
    avg_baseline: int = 20,
) -> bool:
    """
    True if the last `lookback` bars (excluding today) contain:
      - at least 3 red bars (close < open), AND
      - at least one red bar whose body is >= body_multiplier x avg body over
        the prior `avg_baseline` bars (the bars BEFORE the pullback window).

    Implements: "three red candles in a row and some big candles too."
    """
    if len(opens) < lookback + avg_baseline + 1 or len(closes) < lookback + avg_baseline + 1:
        return False

    pullback_opens  = opens.iloc[-(lookback + 1):-1]
    pullback_closes = closes.iloc[-(lookback + 1):-1]

    is_red = (pullback_closes < pullback_opens)
    if int(is_red.sum()) < 3:
        return False

    # Baseline avg body computed from the bars BEFORE the pullback window.
    base_opens  = opens.iloc[-(lookback + avg_baseline + 1):-(lookback + 1)]
    base_closes = closes.iloc[-(lookback + avg_baseline + 1):-(lookback + 1)]
    base_bodies = (base_opens - base_closes).abs()
    avg_body = float(base_bodies.mean())
    if avg_body <= 0:
        return False

    red_bodies = (pullback_opens - pullback_closes)[is_red]  # red => open > close, positive
    return bool((red_bodies >= body_multiplier * avg_body).any())
