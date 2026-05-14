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
