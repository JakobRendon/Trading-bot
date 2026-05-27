"""
Mean Reversion + Divergence strategy.

Per the plan (Phase 5):
- Best pairs: EUR/GBP (top pick — rarely trends), USD/JPY
- Entry LONG: close < lower Bollinger Band AND RSI < 35
- Entry SHORT: close > upper Bollinger Band AND RSI > 65
- Stop-loss: ATR-based distance (proxy for "recent swing high/low")
- Take-profit: configurable R:R (default 2:1, per plan)
- Higher timeframe trend filter — DEFERRED (requires multi-TF support)
- MACD divergence confirmation — DEFERRED (complex swing detection)

Both deferred features are reasonable Slice 5 follow-ups. Their absence
means this v1 will take some counter-trend trades — backtesting should
quantify the cost.
"""

from decimal import Decimal

from strategy import Strategy, Signal
from risk import pip_size
from indicators import bollinger_bands, rsi, atr


class MeanReversionStrategy(Strategy):
    def __init__(
        self,
        instrument="EUR_GBP",
        granularity="H1",
        bb_period=20,
        bb_std_devs=2,
        rsi_period=14,
        rsi_oversold=35,
        rsi_overbought=65,
        atr_period=14,
        sl_atr_multiplier=Decimal("1.5"),
        tp_rr=Decimal("2"),
        min_rr=Decimal("1.5"),
    ):
        self._instrument = instrument
        self._granularity = granularity
        self.bb_period = bb_period
        self.bb_std_devs = bb_std_devs
        self.rsi_period = rsi_period
        self.rsi_oversold = Decimal(str(rsi_oversold))
        self.rsi_overbought = Decimal(str(rsi_overbought))
        self.atr_period = atr_period
        self.sl_atr_multiplier = Decimal(str(sl_atr_multiplier))
        self.tp_rr = Decimal(str(tp_rr))
        self.min_rr = Decimal(str(min_rr))

    @property
    def instrument(self):
        return self._instrument

    @property
    def granularity(self):
        return self._granularity

    @property
    def history_size(self):
        # Need enough for the longest indicator + a buffer for ATR's
        # (period+1) requirement and stable initial values.
        longest = max(self.bb_period, self.rsi_period, self.atr_period)
        return longest * 3  # 3x gives stable smoothed values

    def on_candle_close(self, candle, history):
        # Indicators want the full series including the just-closed candle.
        series = history + [candle]

        bb = bollinger_bands(series, period=self.bb_period, std_devs=self.bb_std_devs)
        if bb is None:
            return None
        rsi_val = rsi(series, period=self.rsi_period)
        if rsi_val is None:
            return None
        atr_val = atr(series, period=self.atr_period)
        if atr_val is None or atr_val <= 0:
            return None

        ps = pip_size(self._instrument)
        sl_distance = atr_val * self.sl_atr_multiplier
        sl_pips = int(sl_distance / ps)
        if sl_pips <= 0:
            return None
        tp_pips = int(Decimal(sl_pips) * self.tp_rr)
        if tp_pips <= 0 or Decimal(tp_pips) / Decimal(sl_pips) < self.min_rr:
            return None

        close = candle["close"]
        if close < bb.lower and rsi_val < self.rsi_oversold:
            return Signal(
                direction="long",
                stop_loss_pips=sl_pips,
                take_profit_pips=tp_pips,
                reason=(
                    f"mean_rev long close={close} lower_bb={bb.lower:.5f} "
                    f"rsi={rsi_val:.1f} atr={atr_val:.5f}"
                ),
            )
        if close > bb.upper and rsi_val > self.rsi_overbought:
            return Signal(
                direction="short",
                stop_loss_pips=sl_pips,
                take_profit_pips=tp_pips,
                reason=(
                    f"mean_rev short close={close} upper_bb={bb.upper:.5f} "
                    f"rsi={rsi_val:.1f} atr={atr_val:.5f}"
                ),
            )
        return None
