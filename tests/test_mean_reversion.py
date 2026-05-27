"""
Unit tests for MeanReversionStrategy.

Most tests mock the indicators (bollinger_bands, rsi, atr) so the strategy's
decision logic can be tested in isolation from indicator math, which has its
own test coverage in test_indicators.py.

A handful of tests use real indicators on hand-crafted candle sequences to
verify the strategy works end-to-end against the actual indicator code.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from indicators import BollingerBands
from mean_reversion import MeanReversionStrategy
from strategy import Signal


def make_candle(close, high=None, low=None, instrument="EUR_GBP", granularity="H1", start_time=0):
    if high is None:
        high = close + 0.0001
    if low is None:
        low = close - 0.0001
    return {
        "instrument": instrument,
        "granularity": granularity,
        "start_time": start_time,
        "open": Decimal(str(close)),
        "high": Decimal(str(high)),
        "low": Decimal(str(low)),
        "close": Decimal(str(close)),
        "volume": 1,
    }


def flat_history(n=60, price=0.85000):
    """Build n flat candles. Long enough to satisfy any indicator's warmup."""
    return [
        make_candle(price, price + 0.0001, price - 0.0001, start_time=i * 3600)
        for i in range(n)
    ]


# --- Strategy properties ---

class TestStrategyProperties:
    def test_default_instrument_is_eur_gbp(self):
        s = MeanReversionStrategy()
        assert s.instrument == "EUR_GBP"

    def test_default_granularity_is_h1(self):
        s = MeanReversionStrategy()
        assert s.granularity == "H1"

    def test_history_size_covers_longest_indicator(self):
        s = MeanReversionStrategy(bb_period=20, rsi_period=14, atr_period=14)
        # Should be at least 3x the longest period (20)
        assert s.history_size >= 60


# --- Long signal ---

class TestLongSignal:
    @patch("mean_reversion.atr", return_value=Decimal("0.00100"))
    @patch("mean_reversion.rsi", return_value=Decimal("25"))
    @patch("mean_reversion.bollinger_bands", return_value=BollingerBands(
        upper=Decimal("0.85200"), middle=Decimal("0.85000"), lower=Decimal("0.84800"),
    ))
    def test_oversold_breakdown_fires_long(self, mock_bb, mock_rsi, mock_atr):
        s = MeanReversionStrategy()
        # close below lower BB AND RSI below 35
        candle = make_candle(close=0.84700)
        signal = s.on_candle_close(candle, flat_history())
        assert signal is not None
        assert signal.direction == "long"

    @patch("mean_reversion.atr", return_value=Decimal("0.00100"))
    @patch("mean_reversion.rsi", return_value=Decimal("40"))  # NOT oversold
    @patch("mean_reversion.bollinger_bands", return_value=BollingerBands(
        upper=Decimal("0.85200"), middle=Decimal("0.85000"), lower=Decimal("0.84800"),
    ))
    def test_bb_break_without_rsi_oversold_no_signal(self, mock_bb, mock_rsi, mock_atr):
        s = MeanReversionStrategy()
        candle = make_candle(close=0.84700)  # below BB, but RSI=40
        signal = s.on_candle_close(candle, flat_history())
        assert signal is None

    @patch("mean_reversion.atr", return_value=Decimal("0.00100"))
    @patch("mean_reversion.rsi", return_value=Decimal("25"))
    @patch("mean_reversion.bollinger_bands", return_value=BollingerBands(
        upper=Decimal("0.85200"), middle=Decimal("0.85000"), lower=Decimal("0.84800"),
    ))
    def test_rsi_oversold_without_bb_break_no_signal(self, mock_bb, mock_rsi, mock_atr):
        s = MeanReversionStrategy()
        candle = make_candle(close=0.84850)  # above lower BB despite RSI=25
        signal = s.on_candle_close(candle, flat_history())
        assert signal is None


# --- Short signal ---

class TestShortSignal:
    @patch("mean_reversion.atr", return_value=Decimal("0.00100"))
    @patch("mean_reversion.rsi", return_value=Decimal("75"))
    @patch("mean_reversion.bollinger_bands", return_value=BollingerBands(
        upper=Decimal("0.85200"), middle=Decimal("0.85000"), lower=Decimal("0.84800"),
    ))
    def test_overbought_breakup_fires_short(self, mock_bb, mock_rsi, mock_atr):
        s = MeanReversionStrategy()
        candle = make_candle(close=0.85300)  # above upper BB, RSI=75
        signal = s.on_candle_close(candle, flat_history())
        assert signal is not None
        assert signal.direction == "short"

    @patch("mean_reversion.atr", return_value=Decimal("0.00100"))
    @patch("mean_reversion.rsi", return_value=Decimal("60"))  # NOT overbought
    @patch("mean_reversion.bollinger_bands", return_value=BollingerBands(
        upper=Decimal("0.85200"), middle=Decimal("0.85000"), lower=Decimal("0.84800"),
    ))
    def test_bb_break_up_without_rsi_overbought_no_signal(self, mock_bb, mock_rsi, mock_atr):
        s = MeanReversionStrategy()
        candle = make_candle(close=0.85300)  # above BB but RSI=60
        signal = s.on_candle_close(candle, flat_history())
        assert signal is None


# --- SL/TP sizing ---

class TestSLTPSizing:
    @patch("mean_reversion.atr", return_value=Decimal("0.00100"))
    @patch("mean_reversion.rsi", return_value=Decimal("25"))
    @patch("mean_reversion.bollinger_bands", return_value=BollingerBands(
        upper=Decimal("0.85200"), middle=Decimal("0.85000"), lower=Decimal("0.84800"),
    ))
    def test_sl_uses_atr_multiplier(self, mock_bb, mock_rsi, mock_atr):
        # ATR=0.00100, multiplier=1.5 → SL distance = 0.00150 = 15 pips
        s = MeanReversionStrategy(sl_atr_multiplier=Decimal("1.5"))
        candle = make_candle(close=0.84700)
        signal = s.on_candle_close(candle, flat_history())
        assert signal.stop_loss_pips == 15

    @patch("mean_reversion.atr", return_value=Decimal("0.00100"))
    @patch("mean_reversion.rsi", return_value=Decimal("25"))
    @patch("mean_reversion.bollinger_bands", return_value=BollingerBands(
        upper=Decimal("0.85200"), middle=Decimal("0.85000"), lower=Decimal("0.84800"),
    ))
    def test_tp_uses_rr_multiplier(self, mock_bb, mock_rsi, mock_atr):
        # SL=15 pips, tp_rr=2 → TP=30 pips
        s = MeanReversionStrategy(tp_rr=Decimal("2"))
        candle = make_candle(close=0.84700)
        signal = s.on_candle_close(candle, flat_history())
        assert signal.take_profit_pips == 30


# --- Filters ---

class TestFilters:
    def test_returns_none_with_insufficient_history(self):
        s = MeanReversionStrategy()
        # Too short for indicators
        signal = s.on_candle_close(make_candle(close=0.84700), [make_candle(close=0.85000)])
        assert signal is None

    @patch("mean_reversion.atr", return_value=Decimal("0"))
    @patch("mean_reversion.rsi", return_value=Decimal("25"))
    @patch("mean_reversion.bollinger_bands", return_value=BollingerBands(
        upper=Decimal("0.85200"), middle=Decimal("0.85000"), lower=Decimal("0.84800"),
    ))
    def test_zero_atr_returns_none(self, mock_bb, mock_rsi, mock_atr):
        # ATR=0 means no volatility — can't size SL meaningfully
        s = MeanReversionStrategy()
        candle = make_candle(close=0.84700)
        signal = s.on_candle_close(candle, flat_history())
        assert signal is None

    @patch("mean_reversion.atr", return_value=Decimal("0.00100"))
    @patch("mean_reversion.rsi", return_value=Decimal("25"))
    @patch("mean_reversion.bollinger_bands", return_value=BollingerBands(
        upper=Decimal("0.85200"), middle=Decimal("0.85000"), lower=Decimal("0.84800"),
    ))
    def test_rr_below_min_returns_none(self, mock_bb, mock_rsi, mock_atr):
        # tp_rr=1.0 → R:R 1.0 < 1.5 min → blocked
        s = MeanReversionStrategy(tp_rr=Decimal("1.0"), min_rr=Decimal("1.5"))
        candle = make_candle(close=0.84700)
        signal = s.on_candle_close(candle, flat_history())
        assert signal is None


# --- Real indicators (end-to-end) ---

class TestRealIndicators:
    def test_flat_market_does_not_fire(self):
        """Perfectly flat candles: BB collapses to a point, RSI undefined,
        ATR may be 0 — strategy should silently produce no signal."""
        s = MeanReversionStrategy()
        history = flat_history(n=80)
        signal = s.on_candle_close(make_candle(close=0.85000), history)
        assert signal is None

    def test_extreme_drop_fires_long(self):
        """Construct a series with calm prices then a sharp drop that pushes
        close below the lower BB and RSI below threshold."""
        # 70 calm candles around 0.85000 with small noise
        history = []
        for i in range(70):
            # Tiny oscillation to give BB a nonzero std
            price = 0.85000 + (0.00005 if i % 2 == 0 else -0.00005)
            history.append(make_candle(close=price, high=price + 0.0001,
                                       low=price - 0.0001, start_time=i * 3600))
        # Now 10 candles dropping sharply
        for i in range(10):
            price = 0.85000 - (i + 1) * 0.0010  # drop 10 pips per candle
            history.append(make_candle(close=price, high=price + 0.0005,
                                       low=price - 0.0010, start_time=(70 + i) * 3600))
        # Final candle drops even harder
        breakdown_close = 0.83800  # ~120 pips below original baseline
        final = make_candle(close=breakdown_close, high=0.84000, low=0.83800,
                            start_time=80 * 3600)
        s = MeanReversionStrategy()
        signal = s.on_candle_close(final, history)
        # Should fire long (oversold + below BB)
        assert signal is not None
        assert signal.direction == "long"
