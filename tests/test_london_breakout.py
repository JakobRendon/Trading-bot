"""
Unit tests for LondonBreakoutStrategy.

Builds synthetic M15 candles with start_times spanning the Asian + London
sessions, then verifies the strategy fires (or doesn't) under various
conditions.

All test times are 2026-05-26 UTC unless otherwise noted.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from london_breakout import LondonBreakoutStrategy
from strategy import Signal


# 2026-05-26 00:00:00 UTC as epoch seconds
DAY_START = datetime(2026, 5, 26, 0, 0, 0, tzinfo=timezone.utc).timestamp()
M15_SECONDS = 15 * 60


def make_candle(hour, minute, high, low, close, instrument="GBP_USD"):
    """Build an M15 candle at the given UTC hour:minute on 2026-05-26."""
    start = DAY_START + (hour * 60 + minute) * 60
    return {
        "instrument": instrument,
        "granularity": "M15",
        "start_time": start,
        "open": Decimal(str(close)),  # arbitrary; we don't read open
        "high": Decimal(str(high)),
        "low": Decimal(str(low)),
        "close": Decimal(str(close)),
        "volume": 10,
    }


def build_asian_session(high=1.2700, low=1.2650, instrument="GBP_USD"):
    """Build a full Asian session (00:00-07:00 UTC) of M15 candles.

    Two candles in the middle hit the session high/low exactly. All other
    candles stay strictly inside the [low, high] range so they don't
    accidentally widen it.
    """
    candles = []
    mid = (high + low) / 2
    for h in range(0, 7):
        for m in (0, 15, 30, 45):
            if h == 3 and m == 30:
                # The candle that defines the session high
                c = make_candle(h, m, high, mid, mid, instrument)
            elif h == 3 and m == 45:
                # The candle that defines the session low
                c = make_candle(h, m, mid, low, mid, instrument)
            else:
                # Degenerate candles strictly inside the range
                c = make_candle(h, m, mid, mid, mid, instrument)
            candles.append(c)
    return candles


# --- Strategy properties ---

class TestStrategyProperties:
    def test_default_instrument_is_gbp_usd(self):
        s = LondonBreakoutStrategy()
        assert s.instrument == "GBP_USD"

    def test_granularity_is_m15(self):
        s = LondonBreakoutStrategy()
        assert s.granularity == "M15"

    def test_history_size_covers_asian_plus_london(self):
        s = LondonBreakoutStrategy()
        # Need at least 28 (Asian) + 8 (London) = 36 candles
        assert s.history_size >= 36


# --- Outside-window behavior ---

class TestOutsideLondonWindow:
    def test_candle_during_asian_session_does_not_fire(self):
        s = LondonBreakoutStrategy()
        history = build_asian_session()
        # Asian candle (e.g., 03:00 UTC) should never fire
        asian_candle = make_candle(3, 0, 1.2700, 1.2650, 1.2675)
        assert s.on_candle_close(asian_candle, history) is None

    def test_candle_after_london_window_does_not_fire(self):
        s = LondonBreakoutStrategy()
        history = build_asian_session()
        # 11:00 UTC = past London window end (10:00)
        late_candle = make_candle(11, 0, 1.2800, 1.2700, 1.2750)
        assert s.on_candle_close(late_candle, history) is None


# --- Range filtering ---

class TestRangeFilters:
    def test_narrow_range_blocks_signal(self):
        # 5 pip range — below default 10 pip floor
        s = LondonBreakoutStrategy(min_range_pips=10)
        history = build_asian_session(high=1.2700, low=1.2695)  # 5 pips
        candle = make_candle(8, 0, 1.2720, 1.2705, 1.2710)
        assert s.on_candle_close(candle, history) is None

    def test_no_breakout_returns_none(self):
        # Range 1.2650-1.2700; close at 1.2680 — inside range
        s = LondonBreakoutStrategy()
        history = build_asian_session(high=1.2700, low=1.2650)
        candle = make_candle(8, 0, 1.2690, 1.2670, 1.2680)
        assert s.on_candle_close(candle, history) is None


# --- Breakout direction ---

class TestBreakoutDirection:
    def test_long_breakout_emits_long_signal(self):
        s = LondonBreakoutStrategy(tp_multiplier=2)
        history = build_asian_session(high=1.2700, low=1.2650)  # 50 pip range
        # Close 10 pips above range high → SL=60, TP=100, R:R=1.67 (passes 1.5)
        candle = make_candle(8, 0, 1.2715, 1.2700, 1.2710)
        signal = s.on_candle_close(candle, history)
        assert signal is not None
        assert signal.direction == "long"

    def test_short_breakout_emits_short_signal(self):
        s = LondonBreakoutStrategy(tp_multiplier=2)
        history = build_asian_session(high=1.2700, low=1.2650)
        # Close 10 pips below range low → SL=60, TP=100, R:R=1.67
        candle = make_candle(8, 0, 1.2650, 1.2635, 1.2640)
        signal = s.on_candle_close(candle, history)
        assert signal is not None
        assert signal.direction == "short"

    def test_sl_uses_opposite_range_side_for_long(self):
        s = LondonBreakoutStrategy(tp_multiplier=2)
        history = build_asian_session(high=1.2700, low=1.2650)
        # Close at 1.2710 (10 pips above range high)
        # SL = close - asian_low = 1.2710 - 1.2650 = 0.0060 = 60 pips
        candle = make_candle(8, 0, 1.2715, 1.2700, 1.2710)
        signal = s.on_candle_close(candle, history)
        assert signal.stop_loss_pips == 60

    def test_tp_uses_range_width_times_multiplier(self):
        s = LondonBreakoutStrategy(tp_multiplier=2)
        history = build_asian_session(high=1.2700, low=1.2650)  # 50 pip range
        candle = make_candle(8, 0, 1.2715, 1.2700, 1.2710)
        signal = s.on_candle_close(candle, history)
        # TP = 2 * 50 pips = 100 pips
        assert signal.take_profit_pips == 100


# --- Risk-reward filter ---

class TestRiskReward:
    def test_signal_below_min_rr_dropped(self):
        # Range = 50 pips, breakout closes 100 pips above range high.
        # SL = 150 pips, TP = 2 * 50 = 100 pips, R:R = 0.67 — below 1.5
        s = LondonBreakoutStrategy(tp_multiplier=2, min_rr=Decimal("1.5"))
        history = build_asian_session(high=1.2700, low=1.2650)
        candle = make_candle(8, 0, 1.2810, 1.2790, 1.2800)
        signal = s.on_candle_close(candle, history)
        assert signal is None

    def test_signal_at_or_above_min_rr_emitted(self):
        # Range = 50 pips, close right at range high (no overshoot).
        # SL = 50 pips, TP = 100 pips, R:R = 2.0 — passes
        s = LondonBreakoutStrategy(tp_multiplier=2, min_rr=Decimal("1.5"))
        history = build_asian_session(high=1.2700, low=1.2650)
        candle = make_candle(8, 0, 1.2710, 1.2698, 1.2701)
        signal = s.on_candle_close(candle, history)
        assert signal is not None


# --- One trade per day ---

class TestOnePerDay:
    def test_second_signal_same_day_blocked_after_fill(self):
        # Once-per-day lockout activates on fill confirmation (on_trade_filled),
        # not on signal emission — so a rejected/cancelled first signal does
        # NOT silently block the rest of the day.
        s = LondonBreakoutStrategy(tp_multiplier=2)
        history = build_asian_session(high=1.2700, low=1.2650)
        c1 = make_candle(8, 0, 1.2715, 1.2700, 1.2710)
        first = s.on_candle_close(c1, history)
        assert first is not None
        # Simulate the runner confirming the fill
        s.on_trade_filled(first, c1)

        history_with_c1 = history + [c1]
        c2 = make_candle(8, 15, 1.2720, 1.2710, 1.2715)
        second = s.on_candle_close(c2, history_with_c1)
        assert second is None

    def test_signal_without_fill_does_not_lock_out_day(self):
        # If the first signal is rejected/cancelled (runner never calls
        # on_trade_filled), the strategy should still be able to emit a
        # later signal that day.
        s = LondonBreakoutStrategy(tp_multiplier=2)
        history = build_asian_session(high=1.2700, low=1.2650)
        c1 = make_candle(8, 0, 1.2715, 1.2700, 1.2710)
        first = s.on_candle_close(c1, history)
        assert first is not None
        # No on_trade_filled — pretend the runner rejected this one.

        history_with_c1 = history + [c1]
        c2 = make_candle(8, 15, 1.2720, 1.2710, 1.2715)
        second = s.on_candle_close(c2, history_with_c1)
        assert second is not None

    def test_next_day_can_trade_again(self):
        s = LondonBreakoutStrategy(tp_multiplier=2)
        history = build_asian_session(high=1.2700, low=1.2650)
        c1 = make_candle(8, 0, 1.2715, 1.2700, 1.2710)
        first = s.on_candle_close(c1, history)
        s.on_trade_filled(first, c1)  # marks today as traded

        # Day 2: 24 hours later
        day2_start = DAY_START + 24 * 3600
        day2_asian = []
        for h in range(0, 7):
            for m in (0, 15, 30, 45):
                start = day2_start + (h * 60 + m) * 60
                day2_asian.append({
                    "instrument": "GBP_USD",
                    "granularity": "M15",
                    "start_time": start,
                    "open": Decimal("1.2700"),
                    "high": Decimal("1.2700") if not (h == 3) else Decimal("1.2710"),
                    "low": Decimal("1.2680") if not (h == 3) else Decimal("1.2670"),
                    "close": Decimal("1.2690"),
                    "volume": 10,
                })
        day2_breakout_time = day2_start + 8 * 3600
        day2_breakout = {
            "instrument": "GBP_USD",
            "granularity": "M15",
            "start_time": day2_breakout_time,
            "open": Decimal("1.2720"),
            "high": Decimal("1.2725"),
            "low": Decimal("1.2715"),
            "close": Decimal("1.2722"),
            "volume": 10,
        }
        signal = s.on_candle_close(day2_breakout, day2_asian)
        # day2 has its own Asian range and is a different date — should fire
        assert signal is not None


# --- Insufficient history ---

class TestInsufficientHistory:
    def test_no_asian_data_returns_none(self):
        s = LondonBreakoutStrategy()
        candle = make_candle(8, 0, 1.2730, 1.2700, 1.2725)
        assert s.on_candle_close(candle, []) is None

    def test_partial_asian_data_returns_none(self):
        # Only 5 Asian candles (need 80% of 28 = ~23)
        s = LondonBreakoutStrategy()
        history = [
            make_candle(h, m, 1.2700, 1.2650, 1.2675)
            for h, m in [(0, 0), (0, 15), (0, 30), (0, 45), (1, 0)]
        ]
        candle = make_candle(8, 0, 1.2730, 1.2700, 1.2725)
        assert s.on_candle_close(candle, history) is None
