from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from strategy import Signal, Strategy, FixedSignalStrategy
from strategy_runner import StrategyRunner
from oanda_api import OandaAPIError, OandaOrderRejected


def make_candle(instrument="EUR_USD", granularity="M1", close="1.10000", start_time=1000):
    return {
        "instrument": instrument,
        "granularity": granularity,
        "start_time": start_time,
        "open": Decimal("1.10000"),
        "high": Decimal("1.10010"),
        "low": Decimal("1.09990"),
        "close": Decimal(close),
        "volume": 10,
    }


def make_guard(allowed=True, reason="", nav="25000.00", open_positions=0):
    g = MagicMock()
    g.can_open_position = MagicMock(return_value=(allowed, reason))
    g.summary = MagicMock(return_value={"current_nav": nav})
    g.open_position_count = MagicMock(return_value=open_positions)
    g.record_position_entry = MagicMock()
    return g


def make_api(response=None, exception=None):
    api = MagicMock()
    if exception:
        api.place_market_order = MagicMock(side_effect=exception)
    else:
        api.place_market_order = MagicMock(
            return_value=response or {"orderFillTransaction": {"price": "1.10000", "id": "42"}}
        )
    return api


# --- Filtering & history ---

class TestCandleFiltering:
    def test_ignores_wrong_instrument(self):
        strategy = FixedSignalStrategy(instrument="EUR_USD")
        runner = StrategyRunner(make_api(), make_guard(), strategy, paper=True)
        runner.on_candle_close("M1", make_candle(instrument="GBP_USD"))
        assert runner.activity == []

    def test_ignores_wrong_granularity(self):
        strategy = FixedSignalStrategy(granularity="M15")
        runner = StrategyRunner(make_api(), make_guard(), strategy, paper=True)
        runner.on_candle_close("M1", make_candle(granularity="M1"))
        assert runner.activity == []

    def test_history_excludes_current_candle(self):
        seen_histories = []

        class CapturingStrategy(Strategy):
            @property
            def instrument(self):
                return "EUR_USD"
            @property
            def granularity(self):
                return "M1"
            def on_candle_close(self, candle, history):
                seen_histories.append(list(history))
                return None

        strategy = CapturingStrategy()
        runner = StrategyRunner(make_api(), make_guard(), strategy, paper=True)

        c1 = make_candle(start_time=1000)
        c2 = make_candle(start_time=1060)
        c3 = make_candle(start_time=1120)

        runner.on_candle_close("M1", c1)
        runner.on_candle_close("M1", c2)
        runner.on_candle_close("M1", c3)

        assert seen_histories[0] == []           # first call: no prior history
        assert seen_histories[1] == [c1]         # second: just c1
        assert seen_histories[2] == [c1, c2]     # third: c1, c2 (not c3)

    def test_history_trimmed_to_history_size(self):
        class ShortHistory(Strategy):
            @property
            def instrument(self):
                return "EUR_USD"
            @property
            def granularity(self):
                return "M1"
            @property
            def history_size(self):
                return 3
            def on_candle_close(self, candle, history):
                return None

        strategy = ShortHistory()
        runner = StrategyRunner(make_api(), make_guard(), strategy, paper=True)
        for i in range(10):
            runner.on_candle_close("M1", make_candle(start_time=1000 + i * 60))

        key = ("EUR_USD", "M1")
        # After 10 candles with history_size=3, only the 3 most recent are retained
        assert len(runner._history[key]) == 3


# --- Paper mode ---

class TestPaperMode:
    def test_paper_mode_logs_signal(self):
        strategy = FixedSignalStrategy()
        runner = StrategyRunner(make_api(), make_guard(), strategy, paper=True)
        runner.on_candle_close("M1", make_candle())
        assert len(runner.activity) == 1
        assert runner.activity[0]["type"] == "paper"
        assert runner.activity[0]["signal"].direction == "long"

    def test_paper_mode_does_not_call_api(self):
        api = make_api()
        strategy = FixedSignalStrategy()
        runner = StrategyRunner(api, make_guard(), strategy, paper=True)
        runner.on_candle_close("M1", make_candle())
        api.place_market_order.assert_not_called()

    def test_paper_mode_does_not_record_entry(self):
        guard = make_guard()
        strategy = FixedSignalStrategy()
        runner = StrategyRunner(make_api(), guard, strategy, paper=True)
        runner.on_candle_close("M1", make_candle())
        guard.record_position_entry.assert_not_called()


# --- Risk guard gating ---

class TestRiskGuardGating:
    def test_blocked_signal_not_executed(self):
        api = make_api()
        guard = make_guard(allowed=False, reason="daily loss buffer hit")
        strategy = FixedSignalStrategy()
        runner = StrategyRunner(api, guard, strategy, paper=False)
        runner.on_candle_close("M1", make_candle())
        api.place_market_order.assert_not_called()
        assert runner.activity[0]["type"] == "blocked"
        assert "daily loss" in runner.activity[0]["reason"]

    def test_blocked_signal_logged_in_paper_mode_too(self):
        guard = make_guard(allowed=False, reason="drawdown")
        strategy = FixedSignalStrategy()
        runner = StrategyRunner(make_api(), guard, strategy, paper=True)
        runner.on_candle_close("M1", make_candle())
        assert runner.activity[0]["type"] == "blocked"


# --- Live execution ---

class TestLiveExecution:
    def test_signal_placed_as_long_buy(self):
        api = make_api()
        strategy = FixedSignalStrategy(direction="long")
        runner = StrategyRunner(api, make_guard(), strategy, paper=False)
        runner.on_candle_close("M1", make_candle())
        call = api.place_market_order.call_args
        # units should be positive for long
        assert call.kwargs.get("stop_loss_pips") == 20
        assert call.kwargs.get("take_profit_pips") == 40
        units = call.args[1] if len(call.args) > 1 else call.kwargs.get("units")
        assert units > 0

    def test_signal_placed_as_short_sell(self):
        api = make_api()
        strategy = FixedSignalStrategy(direction="short")
        runner = StrategyRunner(api, make_guard(), strategy, paper=False)
        runner.on_candle_close("M1", make_candle())
        units = api.place_market_order.call_args.args[1]
        assert units < 0

    def test_signal_with_explicit_units_uses_them(self):
        class ExplicitUnits(FixedSignalStrategy):
            def on_candle_close(self, candle, history):
                return Signal(direction="long", stop_loss_pips=20,
                              take_profit_pips=40, units=500, reason="test")
        api = make_api()
        runner = StrategyRunner(api, make_guard(), ExplicitUnits(), paper=False)
        runner.on_candle_close("M1", make_candle())
        units = api.place_market_order.call_args.args[1]
        assert units == 500

    def test_records_position_entry_on_fill(self):
        api = make_api()
        guard = make_guard()
        strategy = FixedSignalStrategy()
        runner = StrategyRunner(api, guard, strategy, paper=False)
        runner.on_candle_close("M1", make_candle())
        guard.record_position_entry.assert_called_once()

    def test_does_not_record_entry_on_fok_cancel(self):
        api = make_api(response={"orderCancelTransaction": {"reason": "MARKET_HALTED"}})
        guard = make_guard()
        strategy = FixedSignalStrategy()
        runner = StrategyRunner(api, guard, strategy, paper=False)
        runner.on_candle_close("M1", make_candle())
        guard.record_position_entry.assert_not_called()
        assert runner.activity[0]["type"] == "cancelled"

    def test_handles_oanda_api_error(self):
        api = make_api(exception=OandaAPIError(503, "service unavailable"))
        guard = make_guard()
        strategy = FixedSignalStrategy()
        runner = StrategyRunner(api, guard, strategy, paper=False)
        # Should not propagate the exception
        runner.on_candle_close("M1", make_candle())
        assert runner.activity[0]["type"] == "error"
        guard.record_position_entry.assert_not_called()

    def test_handles_order_rejected(self):
        reject = {"type": "MARKET_ORDER_REJECT", "rejectReason": "INSUFFICIENT_MARGIN"}
        api = make_api(exception=OandaOrderRejected(reject))
        guard = make_guard()
        strategy = FixedSignalStrategy()
        runner = StrategyRunner(api, guard, strategy, paper=False)
        runner.on_candle_close("M1", make_candle())
        assert runner.activity[0]["type"] == "error"
        assert "INSUFFICIENT_MARGIN" in runner.activity[0]["error"]

    def test_handles_zero_computed_units(self):
        """Position sizing returning 0 (tiny account, huge SL) shouldn't submit."""
        api = make_api()
        guard = make_guard(nav="10.00")  # tiny NAV
        class BigSL(FixedSignalStrategy):
            def on_candle_close(self, candle, history):
                return Signal(direction="long", stop_loss_pips=10000,
                              take_profit_pips=20000, reason="test")
        runner = StrategyRunner(api, guard, BigSL(), paper=False, default_risk_pct=0.01)
        runner.on_candle_close("M1", make_candle())
        api.place_market_order.assert_not_called()


# --- Error tolerance ---

class TestErrorTolerance:
    def test_strategy_exception_does_not_crash_runner(self):
        class Buggy(Strategy):
            @property
            def instrument(self):
                return "EUR_USD"
            @property
            def granularity(self):
                return "M1"
            def on_candle_close(self, candle, history):
                raise RuntimeError("strategy bug")
        runner = StrategyRunner(make_api(), make_guard(), Buggy(), paper=True)
        # Should not propagate — strategy bugs shouldn't take down the stream
        runner.on_candle_close("M1", make_candle())
        assert runner.activity == []  # no signal, but no crash either
