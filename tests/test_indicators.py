from decimal import Decimal
import pytest

from indicators import (
    sma, ema, rsi, atr, bollinger_bands, macd,
    BollingerBands, MACDResult,
)


def make_candle(open_, high, low, close, volume=1):
    """Build a candle dict with Decimal OHLC matching CandleAggregator output."""
    return {
        "instrument": "EUR_USD",
        "granularity": "M1",
        "start_time": 0,
        "open": Decimal(str(open_)),
        "high": Decimal(str(high)),
        "low": Decimal(str(low)),
        "close": Decimal(str(close)),
        "volume": volume,
    }


def closes_only(prices):
    """Build a candle list from a sequence of close prices (open=high=low=close)."""
    return [make_candle(p, p, p, p) for p in prices]


# --- SMA ---

class TestSMA:
    def test_sma_of_arithmetic_sequence(self):
        # SMA(5) of [1..10] = mean of [6,7,8,9,10] = 8
        result = sma(closes_only(range(1, 11)), period=5)
        assert result == Decimal(8)

    def test_sma_of_constant_series(self):
        result = sma(closes_only([100] * 10), period=5)
        assert result == Decimal(100)

    def test_sma_insufficient_history_returns_none(self):
        assert sma(closes_only([1, 2, 3]), period=5) is None

    def test_sma_exact_period_works(self):
        result = sma(closes_only([1, 2, 3, 4, 5]), period=5)
        assert result == Decimal(3)


# --- EMA ---

class TestEMA:
    def test_ema_of_constant_series(self):
        result = ema(closes_only([100] * 20), period=5)
        assert result == Decimal(100)

    def test_ema_of_arithmetic_sequence(self):
        # Computed by hand: EMA(5) of [1..10] = 8 (see indicators.py docstring math)
        result = ema(closes_only(range(1, 11)), period=5)
        assert result == Decimal(8)

    def test_ema_insufficient_history_returns_none(self):
        assert ema(closes_only([1, 2, 3]), period=5) is None

    def test_ema_uses_sma_seed(self):
        # With exactly `period` candles, EMA equals SMA (seed only)
        result = ema(closes_only([2, 4, 6, 8, 10]), period=5)
        # SMA = 6 → EMA = 6 (no smoothing applied yet)
        assert result == Decimal(6)


# --- RSI ---

class TestRSI:
    def test_rsi_all_gains_returns_100(self):
        result = rsi(closes_only(range(1, 16)), period=14)
        assert result == Decimal(100)

    def test_rsi_all_losses_returns_0(self):
        result = rsi(closes_only(range(15, 0, -1)), period=14)
        assert result == Decimal(0)

    def test_rsi_insufficient_history_returns_none(self):
        # Need period + 1 candles for `period` changes
        assert rsi(closes_only(range(1, 15)), period=14) is None  # only 14 candles

    def test_rsi_minimum_history_works(self):
        # 15 candles → 14 changes — just enough for period=14
        result = rsi(closes_only(range(1, 16)), period=14)
        assert result is not None

    def test_rsi_balanced_changes_near_50(self):
        # Alternating +1/-1 movements → roughly equal gains and losses → RSI ~50.
        # Tolerance accounts for Wilder smoothing not converging to exactly 50
        # unless the series is long enough for the seed bias to wash out.
        closes = [10]
        for i in range(20):
            closes.append(closes[-1] + (1 if i % 2 == 0 else -1))
        result = rsi(closes_only(closes), period=14)
        assert abs(result - Decimal(50)) < Decimal("2.0")


# --- ATR ---

class TestATR:
    def test_atr_constant_range_returns_range(self):
        # Each candle has H-L = 1, no gaps → TR = 1, ATR = 1
        candles = [make_candle(10, 11, 10, 10.5) for _ in range(20)]
        result = atr(candles, period=14)
        assert result == Decimal(1)

    def test_atr_insufficient_history_returns_none(self):
        # Need period + 1 candles
        candles = [make_candle(10, 11, 10, 10.5) for _ in range(10)]
        assert atr(candles, period=14) is None

    def test_atr_accounts_for_gap_up(self):
        # First candle: H=10, L=9, C=9.5
        # Second candle gaps up: H=12, L=11, C=11.5
        # TR for 2nd = max(12-11, |12-9.5|, |11-9.5|) = max(1, 2.5, 1.5) = 2.5
        candles = [make_candle(9.5, 10, 9, 9.5)]
        for _ in range(15):
            candles.append(make_candle(11.5, 12, 11, 11.5))
        result = atr(candles, period=14)
        # First gap TR=2.5, rest TR=1 — ATR shouldn't be just 1
        assert result > Decimal(1)


# --- Bollinger Bands ---

class TestBollingerBands:
    def test_bb_of_constant_series(self):
        # All same closes → std = 0, upper = middle = lower
        result = bollinger_bands(closes_only([100] * 20), period=20)
        assert result.middle == Decimal(100)
        assert result.upper == Decimal(100)
        assert result.lower == Decimal(100)

    def test_bb_alternating_series(self):
        # 20 alternating values 9,11,9,11,... → mean=10, population std=1
        result = bollinger_bands(closes_only([9, 11] * 10), period=20, std_devs=2)
        assert result.middle == Decimal(10)
        assert result.upper == Decimal(12)
        assert result.lower == Decimal(8)

    def test_bb_insufficient_history_returns_none(self):
        assert bollinger_bands(closes_only([1, 2, 3]), period=20) is None

    def test_bb_returns_namedtuple(self):
        result = bollinger_bands(closes_only([100] * 20))
        assert isinstance(result, BollingerBands)
        assert result.upper >= result.middle >= result.lower


# --- MACD ---

class TestMACD:
    def test_macd_of_constant_series_is_zero(self):
        # Flat series → both EMAs = constant → MACD = 0
        candles = closes_only([100] * 50)
        result = macd(candles, fast=12, slow=26, signal=9)
        assert result.macd == Decimal(0)
        assert result.signal == Decimal(0)
        assert result.histogram == Decimal(0)

    def test_macd_insufficient_history_returns_none(self):
        # Need slow + signal - 1 = 34 candles minimum
        assert macd(closes_only(range(1, 30))) is None

    def test_macd_rising_series_macd_positive(self):
        # Steady rising prices: fast EMA > slow EMA → MACD positive
        candles = closes_only(range(1, 60))
        result = macd(candles)
        assert result.macd > Decimal(0)

    def test_macd_falling_series_macd_negative(self):
        candles = closes_only(range(60, 0, -1))
        result = macd(candles)
        assert result.macd < Decimal(0)

    def test_macd_returns_namedtuple(self):
        candles = closes_only([100] * 50)
        result = macd(candles)
        assert isinstance(result, MACDResult)

    def test_macd_custom_periods_5_34_5(self):
        """The plan's mean reversion strategy uses MACD(5, 34, 5)."""
        candles = closes_only([100] * 50)
        result = macd(candles, fast=5, slow=34, signal=5)
        assert result.macd == Decimal(0)


# --- Cross-cutting: empty input ---

class TestEmptyInput:
    def test_all_indicators_handle_empty_candles(self):
        empty = []
        assert sma(empty, 5) is None
        assert ema(empty, 5) is None
        assert rsi(empty, 14) is None
        assert atr(empty, 14) is None
        assert bollinger_bands(empty) is None
        assert macd(empty) is None
