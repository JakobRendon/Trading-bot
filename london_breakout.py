"""
London Breakout strategy.

Identifies the Asian-session range (default 00:00-07:00 UTC) and triggers an
entry on the first M15 candle that closes beyond it during the London open
window (default 08:00-10:00 UTC). One trade per UTC day maximum.

Per the plan:
- Best pairs: GBP/USD (top pick), EUR/USD, USD/JPY
- Stop-loss: opposite side of the Asian range
- Take-profit: 1.5x to 2x the range width (configurable)
- Skip setups where the Asian range is too narrow (default 10 pips;
  plan suggests 80 pips on GBP/USD)
- Risk-reward minimum 1:1.5 — signals failing this are dropped
  (place_market_order would reject them anyway)
"""

from datetime import datetime, timezone
from decimal import Decimal

from strategy import Strategy, Signal
from risk import pip_size


class LondonBreakoutStrategy(Strategy):
    def __init__(
        self,
        instrument="GBP_USD",
        asian_start_hour=0,
        asian_end_hour=7,
        london_window_start=8,
        london_window_end=10,
        tp_multiplier=Decimal("2"),
        min_range_pips=10,
        min_rr=Decimal("1.5"),
    ):
        self._instrument = instrument
        self.asian_start_hour = asian_start_hour
        self.asian_end_hour = asian_end_hour
        self.london_window_start = london_window_start
        self.london_window_end = london_window_end
        self.tp_multiplier = Decimal(str(tp_multiplier))
        self.min_range_pips = min_range_pips
        self.min_rr = Decimal(str(min_rr))
        # Tracks the UTC date of the last trade to enforce one-per-day.
        self._last_trade_date = None

    @property
    def instrument(self):
        return self._instrument

    @property
    def granularity(self):
        return "M15"

    @property
    def history_size(self):
        # Asian session = 7h * 4 candles/h = 28; London window adds another 8.
        # 64 gives ~16h, comfortably covering both with margin.
        return 64

    def on_candle_close(self, candle, history):
        candle_dt = datetime.fromtimestamp(candle["start_time"], tz=timezone.utc)
        candle_hour = candle_dt.hour
        candle_date = candle_dt.date()

        # Skip if we already traded today.
        if self._last_trade_date == candle_date:
            return None

        # Only consider candles within the London entry window.
        if not (self.london_window_start <= candle_hour < self.london_window_end):
            return None

        # Find Asian candles in history matching today's UTC date.
        asian_candles = [
            c for c in history if self._is_in_asian_session(c, candle_date)
        ]
        # Need enough coverage — at least 80% of the Asian session present.
        expected_asian = (self.asian_end_hour - self.asian_start_hour) * 4  # M15
        if len(asian_candles) < int(expected_asian * 0.8):
            return None

        asian_high = max(c["high"] for c in asian_candles)
        asian_low = min(c["low"] for c in asian_candles)
        range_size = asian_high - asian_low

        # Filter too-narrow ranges.
        ps = pip_size(self._instrument)
        range_pips = range_size / ps
        if range_pips < Decimal(self.min_range_pips):
            return None

        close = candle["close"]
        if close > asian_high:
            direction = "long"
            sl_distance = close - asian_low
        elif close < asian_low:
            direction = "short"
            sl_distance = asian_high - close
        else:
            return None  # No breakout

        tp_distance = range_size * self.tp_multiplier

        # Risk-reward filter — must meet plan's 1:1.5 minimum.
        if sl_distance <= 0 or tp_distance / sl_distance < self.min_rr:
            return None

        sl_pips = int(sl_distance / ps)
        tp_pips = int(tp_distance / ps)
        if sl_pips <= 0 or tp_pips <= 0:
            return None

        # Note: _last_trade_date is set in on_trade_filled, NOT here. Setting
        # it eagerly would silently lock the strategy out for the rest of the
        # day if the guard rejects the signal or OANDA returns a FOK cancel.
        return Signal(
            direction=direction,
            stop_loss_pips=sl_pips,
            take_profit_pips=tp_pips,
            reason=(
                f"london_breakout {direction} "
                f"asian_high={asian_high} asian_low={asian_low} "
                f"close={close} range_pips={int(range_pips)}"
            ),
        )

    def on_trade_filled(self, signal, candle):
        candle_dt = datetime.fromtimestamp(candle["start_time"], tz=timezone.utc)
        self._last_trade_date = candle_dt.date()

    def _is_in_asian_session(self, candle, target_date):
        candle_dt = datetime.fromtimestamp(candle["start_time"], tz=timezone.utc)
        if candle_dt.date() != target_date:
            return False
        return self.asian_start_hour <= candle_dt.hour < self.asian_end_hour
