"""
Technical indicators for strategy use.

All functions accept a list of candles (the dicts emitted by CandleAggregator
with Decimal OHLC) and return the indicator value at the most recent candle.
Functions return None if there isn't enough history.

Indicators implemented (per Trading_Bot_Plan.md Phase 5):
- SMA: simple moving average of closes
- EMA: exponential moving average with SMA seed
- RSI: Wilder's smoothing of gains/losses
- ATR: Wilder's smoothing of true range
- Bollinger Bands: SMA(20) ± 2 population-std-devs
- MACD: EMA(fast) - EMA(slow), signal is EMA of MACD

Decimal arithmetic throughout — no float drift. For backtests with millions
of candles, swap in numpy/pandas later; for live strategies on 200-candle
windows the cost is negligible.
"""

from collections import namedtuple
from decimal import Decimal


BollingerBands = namedtuple("BollingerBands", ["upper", "middle", "lower"])
MACDResult = namedtuple("MACDResult", ["macd", "signal", "histogram"])


def _closes(candles):
    return [c["close"] for c in candles]


def _decimal_sqrt(value):
    """Decimal square root via the .sqrt() method (introduced in Python 3.0)."""
    return Decimal(value).sqrt()


# --- Moving averages ---

def sma(candles, period):
    """Simple Moving Average of the last `period` candle closes."""
    if len(candles) < period:
        return None
    closes = _closes(candles[-period:])
    return sum(closes) / Decimal(period)


def _ema_series_values(values, period):
    """EMA series over raw values using SMA-seed initialization.

    Returns a list of length len(values), with None for indices < period-1
    (where there isn't enough history for the seed).
    """
    if len(values) < period:
        return [None] * len(values)
    series = [None] * (period - 1)
    seed = sum(values[:period]) / Decimal(period)
    series.append(seed)
    alpha = Decimal(2) / Decimal(period + 1)
    for value in values[period:]:
        prev = series[-1]
        series.append(alpha * value + (Decimal(1) - alpha) * prev)
    return series


def ema(candles, period):
    """Exponential Moving Average with SMA-seed initialization.

    α = 2/(period+1). First `period-1` candles have no EMA; the period-th
    candle's EMA seeds from SMA(period).
    """
    if len(candles) < period:
        return None
    return _ema_series_values(_closes(candles), period)[-1]


# --- RSI ---

def rsi(candles, period=14):
    """Relative Strength Index using Wilder's smoothing (α = 1/period).

    Needs at least `period + 1` candles (period changes from period+1 closes).
    Returns Decimal in [0, 100], or None.
    """
    if len(candles) < period + 1:
        return None
    closes = _closes(candles)
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # Initial average: simple mean of first `period` changes
    gains = [c if c > 0 else Decimal(0) for c in changes[:period]]
    losses = [-c if c < 0 else Decimal(0) for c in changes[:period]]
    avg_gain = sum(gains) / Decimal(period)
    avg_loss = sum(losses) / Decimal(period)

    # Wilder smoothing for subsequent changes
    for change in changes[period:]:
        gain = change if change > 0 else Decimal(0)
        loss = -change if change < 0 else Decimal(0)
        avg_gain = (avg_gain * Decimal(period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + loss) / Decimal(period)

    if avg_loss == 0:
        # No losses observed — RSI is 100 (overbought extreme)
        return Decimal(100) if avg_gain > 0 else Decimal(50)
    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


# --- ATR ---

def _true_range(high, low, prev_close):
    return max(
        high - low,
        abs(high - prev_close),
        abs(low - prev_close),
    )


def atr(candles, period=14):
    """Average True Range using Wilder's smoothing.

    Needs at least `period + 1` candles (TR needs a previous close).
    Returns Decimal or None.
    """
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        trs.append(_true_range(
            candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        ))
    avg = sum(trs[:period]) / Decimal(period)
    for tr in trs[period:]:
        avg = (avg * Decimal(period - 1) + tr) / Decimal(period)
    return avg


# --- Bollinger Bands ---

def bollinger_bands(candles, period=20, std_devs=2):
    """Bollinger Bands: SMA(period) ± std_devs * population standard deviation.

    Returns a BollingerBands namedtuple (upper, middle, lower) or None.
    Uses population std (ddof=0), matching most BB implementations.
    """
    if len(candles) < period:
        return None
    closes = _closes(candles[-period:])
    mean = sum(closes) / Decimal(period)
    variance = sum((c - mean) ** 2 for c in closes) / Decimal(period)
    std = _decimal_sqrt(variance)
    delta = Decimal(std_devs) * std
    return BollingerBands(
        upper=mean + delta,
        middle=mean,
        lower=mean - delta,
    )


# --- MACD ---

def macd(candles, fast=12, slow=26, signal=9):
    """Moving Average Convergence/Divergence.

    Returns a MACDResult (macd, signal, histogram), or None.
    Plan defaults: fast=12, slow=26, signal=9 (standard). Mean reversion
    strategy uses (5, 34, 5) per the plan — override accordingly.
    """
    if len(candles) < slow + signal - 1:
        return None
    closes = _closes(candles)
    fast_series = _ema_series_values(closes, fast)
    slow_series = _ema_series_values(closes, slow)
    macd_line_series = [
        f - s if f is not None and s is not None else None
        for f, s in zip(fast_series, slow_series)
    ]
    # Build signal line from the non-None part of macd_line_series
    valid_macd = [v for v in macd_line_series if v is not None]
    if len(valid_macd) < signal:
        return None
    signal_series = _ema_series_values(valid_macd, signal)
    signal_line = signal_series[-1]
    macd_value = macd_line_series[-1]
    return MACDResult(
        macd=macd_value,
        signal=signal_line,
        histogram=macd_value - signal_line,
    )
