"""
Unit tests for the backtest engine.

Uses small synthetic candle scripts and minimal strategies to verify:
- SL/TP hit detection in subsequent candles
- Both-hit-same-candle conservative assumption (SL wins)
- Position open at end of data is closed at last close
- Win/loss/PF/drawdown math
- OANDA candle normalization
"""

from decimal import Decimal
import pytest

from strategy import Strategy, Signal
from backtest import (
    Backtester, BacktestResult, SimulatedTrade, normalize_oanda_candle,
    WalkForwardAnalyzer, WalkForwardResult, WindowResult,
)


def make_candle(start, open_, high, low, close, instrument="EUR_USD"):
    return {
        "instrument": instrument,
        "granularity": "M1",
        "start_time": start,
        "open": Decimal(str(open_)),
        "high": Decimal(str(high)),
        "low": Decimal(str(low)),
        "close": Decimal(str(close)),
        "volume": 1,
    }


class OneShotLongStrategy(Strategy):
    """Emits a long signal on the first candle only; never again.

    Useful for testing single-trade lifecycles end to end.
    """

    def __init__(self, sl_pips=20, tp_pips=40, instrument="EUR_USD"):
        self._instrument = instrument
        self._sl = sl_pips
        self._tp = tp_pips
        self._fired = False

    @property
    def instrument(self):
        return self._instrument

    @property
    def granularity(self):
        return "M1"

    @property
    def history_size(self):
        return 50

    def on_candle_close(self, candle, history):
        if self._fired:
            return None
        self._fired = True
        return Signal(
            direction="long",
            stop_loss_pips=self._sl,
            take_profit_pips=self._tp,
            units=1000,
            reason="oneshot-long",
        )


class OneShotShortStrategy(OneShotLongStrategy):
    def on_candle_close(self, candle, history):
        if self._fired:
            return None
        self._fired = True
        return Signal(
            direction="short",
            stop_loss_pips=self._sl,
            take_profit_pips=self._tp,
            units=1000,
            reason="oneshot-short",
        )


# --- Trade lifecycle (long) ---

class TestLongTradeLifecycle:
    def test_long_tp_hit_in_later_candle(self):
        # Entry at close=1.10000 with TP=40 pips → TP price = 1.10400
        # Candle 2 high=1.10500 hits TP
        candles = [
            make_candle(0, 1.09950, 1.10010, 1.09950, 1.10000),
            make_candle(60, 1.10000, 1.10500, 1.09990, 1.10200),
        ]
        strategy = OneShotLongStrategy(sl_pips=20, tp_pips=40)
        result = Backtester(strategy).run(candles)
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exit_reason == "tp"
        assert trade.pl > 0

    def test_long_sl_hit_in_later_candle(self):
        # Entry at close=1.10000 with SL=20 pips → SL price = 1.09800
        # Candle 2 low=1.09700 hits SL
        candles = [
            make_candle(0, 1.09990, 1.10010, 1.09980, 1.10000),
            make_candle(60, 1.10000, 1.10010, 1.09700, 1.09750),
        ]
        strategy = OneShotLongStrategy(sl_pips=20, tp_pips=40)
        result = Backtester(strategy).run(candles)
        trade = result.trades[0]
        assert trade.exit_reason == "sl"
        assert trade.pl < 0

    def test_long_both_sl_and_tp_in_same_candle_assumes_sl(self):
        # Entry close=1.10000, SL=20 (price 1.09800), TP=40 (price 1.10400)
        # Candle 2: low=1.09700 (hits SL), high=1.10500 (hits TP)
        # Conservative: SL fires first
        candles = [
            make_candle(0, 1.09990, 1.10010, 1.09980, 1.10000),
            make_candle(60, 1.10000, 1.10500, 1.09700, 1.10100),
        ]
        strategy = OneShotLongStrategy(sl_pips=20, tp_pips=40)
        result = Backtester(strategy).run(candles)
        trade = result.trades[0]
        assert trade.exit_reason == "sl"

    def test_long_open_at_end_closes_at_last_close(self):
        # SL=50 pips → 1.09500, TP=100 pips → 1.11000. Neither hit.
        # Trade still open at end → closes at last close.
        candles = [
            make_candle(0, 1.09990, 1.10005, 1.09990, 1.10000),
            make_candle(60, 1.10000, 1.10010, 1.09990, 1.10005),
        ]
        strategy = OneShotLongStrategy(sl_pips=50, tp_pips=100)
        result = Backtester(strategy).run(candles)
        trade = result.trades[0]
        assert trade.exit_reason == "end"
        assert trade.exit_price == Decimal("1.10005")


# --- Trade lifecycle (short) ---

class TestShortTradeLifecycle:
    def test_short_tp_hit(self):
        # Entry close=1.10000, SL=20 → SL price 1.10200, TP=40 → TP price 1.09600
        # Candle 2 low=1.09500 hits TP
        candles = [
            make_candle(0, 1.10010, 1.10020, 1.09990, 1.10000),
            make_candle(60, 1.10000, 1.10005, 1.09500, 1.09700),
        ]
        strategy = OneShotShortStrategy(sl_pips=20, tp_pips=40)
        result = Backtester(strategy).run(candles)
        trade = result.trades[0]
        assert trade.exit_reason == "tp"
        assert trade.pl > 0

    def test_short_sl_hit(self):
        # Entry close=1.10000, SL=20 → SL price 1.10200
        # Candle 2 high=1.10300 hits SL
        candles = [
            make_candle(0, 1.10010, 1.10015, 1.09990, 1.10000),
            make_candle(60, 1.10000, 1.10300, 1.09990, 1.10250),
        ]
        strategy = OneShotShortStrategy(sl_pips=20, tp_pips=40)
        result = Backtester(strategy).run(candles)
        trade = result.trades[0]
        assert trade.exit_reason == "sl"
        assert trade.pl < 0


# --- Position sizing & no-open-during-trade ---

class TestBacktesterBehavior:
    def test_signal_ignored_while_position_open(self):
        # Strategy emits long every candle; only the first one should fill.
        class AlwaysLong(Strategy):
            @property
            def instrument(self):
                return "EUR_USD"
            @property
            def granularity(self):
                return "M1"
            def on_candle_close(self, candle, history):
                return Signal(
                    direction="long", stop_loss_pips=100, take_profit_pips=200,
                    units=1000, reason="always",
                )
        candles = [
            make_candle(0, 1.09990, 1.10010, 1.09990, 1.10000),
            make_candle(60, 1.10000, 1.10010, 1.09990, 1.10000),
            make_candle(120, 1.10000, 1.10010, 1.09990, 1.10000),
        ]
        result = Backtester(AlwaysLong()).run(candles)
        # Only one trade is opened (others ignored because position is open)
        assert len(result.trades) == 1

    def test_explicit_units_honored(self):
        candles = [
            make_candle(0, 1.09990, 1.10010, 1.09990, 1.10000),
            make_candle(60, 1.10000, 1.10010, 1.09990, 1.10005),
        ]
        strategy = OneShotLongStrategy(sl_pips=50, tp_pips=100)
        result = Backtester(strategy).run(candles)
        assert result.trades[0].units == 1000

    def test_empty_candles_returns_no_trades(self):
        result = Backtester(OneShotLongStrategy()).run([])
        assert result.num_trades == 0
        assert result.final_balance == Decimal("25000")


# --- Result metrics ---

class TestResultMetrics:
    def _result_with(self, pls):
        """Helper: build a BacktestResult with closed trades having the given P/Ls."""
        trades = []
        for i, pl in enumerate(pls):
            trades.append(SimulatedTrade(
                direction="long", units=100, entry_time=i, entry_price=Decimal("1.10000"),
                sl_price=Decimal("1.09000"), tp_price=Decimal("1.11000"),
                exit_time=i + 1, exit_price=Decimal("1.10100"),
                exit_reason="tp" if pl > 0 else "sl", pl=Decimal(str(pl)),
            ))
        start = Decimal("10000")
        final = start + sum(Decimal(str(p)) for p in pls)
        return BacktestResult(
            trades=trades,
            starting_balance=start,
            final_balance=final,
            equity_curve=[start + sum(Decimal(str(p)) for p in pls[:i]) for i in range(len(pls) + 1)],
        )

    def test_win_rate(self):
        r = self._result_with([100, -50, 75, -25, 100])  # 3 wins, 2 losses
        assert r.win_rate == Decimal(3) / Decimal(5)

    def test_profit_factor(self):
        # Gross profit = 100+75+100 = 275; gross loss = |50+25| = 75; PF = 275/75
        r = self._result_with([100, -50, 75, -25, 100])
        assert r.profit_factor == Decimal(275) / Decimal(75)

    def test_profit_factor_none_when_no_losses(self):
        r = self._result_with([100, 50, 75])
        assert r.profit_factor is None  # Undefined — no losses

    def test_max_drawdown_pct(self):
        # Equity: 10000 -> 11000 -> 9000 -> 11000 -> 8000
        # Peak 11000, trough 8000 → DD = 3000/11000 = 27.27%
        r = BacktestResult(
            trades=[],
            starting_balance=Decimal("10000"),
            final_balance=Decimal("8000"),
            equity_curve=[
                Decimal("10000"), Decimal("11000"), Decimal("9000"),
                Decimal("11000"), Decimal("8000"),
            ],
        )
        assert abs(r.max_drawdown_pct - Decimal("27.27")) < Decimal("0.01")

    def test_total_pl(self):
        r = self._result_with([100, -50, 200])
        assert r.total_pl == Decimal(250)


# --- OANDA candle normalization ---

class TestNormalizeOandaCandle:
    def test_normalizes_mid_prices(self):
        oanda = {
            "time": "2024-01-01T00:00:00.000000000Z",
            "volume": 100,
            "mid": {"o": "1.10000", "h": "1.10100", "l": "1.09950", "c": "1.10050"},
        }
        result = normalize_oanda_candle(oanda, "EUR_USD", "H1")
        assert result["instrument"] == "EUR_USD"
        assert result["granularity"] == "H1"
        assert result["open"] == Decimal("1.10000")
        assert result["high"] == Decimal("1.10100")
        assert result["low"] == Decimal("1.09950")
        assert result["close"] == Decimal("1.10050")
        assert result["volume"] == 100

    def test_handles_nanosecond_timestamps(self):
        # 9-digit fractional seconds → must trim to microseconds
        oanda = {
            "time": "2024-01-01T00:00:00.123456789Z",
            "volume": 50,
            "mid": {"o": "1.0", "h": "1.0", "l": "1.0", "c": "1.0"},
        }
        result = normalize_oanda_candle(oanda, "EUR_USD", "M1")
        # Don't crash — just verify epoch is reasonable
        assert result["start_time"] > 0

    def test_handles_no_fractional_seconds(self):
        oanda = {
            "time": "2024-01-01T00:00:00Z",
            "volume": 10,
            "mid": {"o": "1.0", "h": "1.0", "l": "1.0", "c": "1.0"},
        }
        result = normalize_oanda_candle(oanda, "EUR_USD", "M1")
        assert result["start_time"] > 0


# --- Walk-forward analysis ---

class CountingStrategy(Strategy):
    """Emits a long signal every Nth candle. Stateful so each WF window
    needs a fresh instance (proves the factory pattern works)."""

    def __init__(self, every_n=10, instrument="EUR_USD"):
        self._instrument = instrument
        self.every_n = every_n
        self._counter = 0

    @property
    def instrument(self):
        return self._instrument

    @property
    def granularity(self):
        return "M1"

    @property
    def history_size(self):
        return 20

    def on_candle_close(self, candle, history):
        self._counter += 1
        if self._counter % self.every_n != 0:
            return None
        return Signal(
            direction="long", stop_loss_pips=20, take_profit_pips=40,
            units=100, reason="counting",
        )


def build_candles(n, start_time=0, granularity_seconds=60, price=1.10000):
    """Build a sequence of flat M1 candles (no trades will trigger SL/TP)."""
    return [
        {
            "instrument": "EUR_USD",
            "granularity": "M1",
            "start_time": start_time + i * granularity_seconds,
            "open": Decimal(str(price)),
            "high": Decimal(str(price)),
            "low": Decimal(str(price)),
            "close": Decimal(str(price)),
            "volume": 1,
        }
        for i in range(n)
    ]


class TestWalkForwardBasics:
    def test_empty_candles_returns_no_windows(self):
        analyzer = WalkForwardAnalyzer(lambda: CountingStrategy())
        result = analyzer.run([], window_size=100)
        assert result.windows == []

    def test_insufficient_data_returns_no_windows(self):
        """Need at least warmup + window_size candles."""
        analyzer = WalkForwardAnalyzer(lambda: CountingStrategy())
        candles = build_candles(50)  # less than default warmup (64) + window
        result = analyzer.run(candles, window_size=100, warmup=64)
        assert result.windows == []

    def test_exactly_one_window_when_data_fits_once(self):
        analyzer = WalkForwardAnalyzer(lambda: CountingStrategy())
        # warmup=10 + window=20 = 30 candles minimum
        candles = build_candles(30)
        result = analyzer.run(candles, window_size=20, warmup=10)
        assert len(result.windows) == 1

    def test_multiple_non_overlapping_windows(self):
        analyzer = WalkForwardAnalyzer(lambda: CountingStrategy())
        # warmup=10 + 3 windows of 20 = 70 candles
        candles = build_candles(70)
        result = analyzer.run(candles, window_size=20, warmup=10)
        # Window 1: idx 10..29, Window 2: 30..49, Window 3: 50..69
        assert len(result.windows) == 3

    def test_factory_creates_fresh_instance_per_window(self):
        # Use a list to count factory invocations
        instances = []

        def factory():
            s = CountingStrategy(every_n=5)
            instances.append(s)
            return s

        analyzer = WalkForwardAnalyzer(factory)
        candles = build_candles(70)
        analyzer.run(candles, window_size=20, warmup=10)
        # 3 windows × 1 factory call per window = 3 instances
        assert len(instances) == 3

    def test_overlapping_windows_via_step(self):
        analyzer = WalkForwardAnalyzer(lambda: CountingStrategy())
        candles = build_candles(100)
        # 50% overlap: window=20, step=10
        result = analyzer.run(candles, window_size=20, step=10, warmup=10)
        # Windows start at indices 10, 20, 30, ... 80; each ends 20 later.
        # Last valid start: 80 (80+20=100 <= 100). Steps: 10,20,30,40,50,60,70,80 = 8 windows
        assert len(result.windows) == 8


class TestWalkForwardMetrics:
    def _make_result_with_pl_per_window(self, window_pls):
        """Build a WalkForwardResult with fixed P/L per window."""
        windows = []
        for i, pl in enumerate(window_pls):
            # Build a trade with the right P/L sign
            trade = SimulatedTrade(
                direction="long", units=100, entry_time=i * 1000,
                entry_price=Decimal("1.10000"),
                sl_price=Decimal("1.09000"), tp_price=Decimal("1.11000"),
                exit_time=i * 1000 + 500, exit_price=Decimal("1.10100"),
                exit_reason="tp" if pl > 0 else "sl", pl=Decimal(str(pl)),
            )
            window_result = BacktestResult(
                trades=[trade],
                starting_balance=Decimal("10000"),
                final_balance=Decimal("10000") + Decimal(str(pl)),
                equity_curve=[],
            )
            windows.append(WindowResult(
                start_time=i * 1000,
                end_time=i * 1000 + 1000,
                result=window_result,
            ))
        return WalkForwardResult(windows=windows)

    def test_total_pl_sums_across_windows(self):
        result = self._make_result_with_pl_per_window([100, -50, 200])
        assert result.total_pl == Decimal(250)

    def test_aggregate_win_rate(self):
        result = self._make_result_with_pl_per_window([100, -50, 200, -25, 300])
        # 3 wins, 2 losses
        assert result.aggregate_win_rate == Decimal(3) / Decimal(5)

    def test_aggregate_profit_factor(self):
        result = self._make_result_with_pl_per_window([100, -50, 200, -25, 300])
        # gross profit = 600, gross loss = 75
        assert result.aggregate_profit_factor == Decimal(600) / Decimal(75)

    def test_profitable_windows_pct(self):
        # 3 of 5 windows profitable = 60%
        result = self._make_result_with_pl_per_window([100, -50, 200, -25, 300])
        assert result.profitable_windows_pct == Decimal(60)

    def test_best_and_worst_window(self):
        result = self._make_result_with_pl_per_window([100, -50, 200, -25, 300])
        assert result.best_window_pl == Decimal(300)
        assert result.worst_window_pl == Decimal(-50)

    def test_no_losses_returns_none_profit_factor(self):
        result = self._make_result_with_pl_per_window([100, 200, 300])
        assert result.aggregate_profit_factor is None


class TestWalkForwardWarmupFiltering:
    def test_warmup_trades_excluded_from_window(self):
        """Trades opened before window_start are filtered out."""
        # Build candles where strategy fires on every candle. With warmup=10,
        # the first window starts at index 10. Trades from candles 0..9 should
        # not count toward window 1's metrics.

        class OnLong(Strategy):
            @property
            def instrument(self):
                return "EUR_USD"
            @property
            def granularity(self):
                return "M1"
            @property
            def history_size(self):
                return 5
            def on_candle_close(self, candle, history):
                return Signal(
                    direction="long", stop_loss_pips=100, take_profit_pips=200,
                    units=100, reason="alwayslong",
                )

        candles = build_candles(30)
        analyzer = WalkForwardAnalyzer(lambda: OnLong())
        result = analyzer.run(candles, window_size=20, warmup=10)
        # One window — trades opened at candles 10..29 only (not 0..9 warmup)
        window = result.windows[0]
        for trade in window.result.trades:
            assert trade.entry_time >= candles[10]["start_time"]
