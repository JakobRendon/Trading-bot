from decimal import Decimal
import pytest

from strategy import Signal, Strategy, FixedSignalStrategy


# --- Signal validation ---

class TestSignalValidation:
    def test_valid_long_signal(self):
        s = Signal(direction="long", stop_loss_pips=20, take_profit_pips=40)
        assert s.direction == "long"
        assert s.stop_loss_pips == 20
        assert s.take_profit_pips == 40

    def test_valid_short_signal(self):
        s = Signal(direction="short", stop_loss_pips=15)
        assert s.direction == "short"
        assert s.take_profit_pips is None

    def test_invalid_direction(self):
        with pytest.raises(ValueError, match="direction"):
            Signal(direction="up", stop_loss_pips=20)

    def test_zero_sl_rejected(self):
        with pytest.raises(ValueError, match="stop_loss_pips"):
            Signal(direction="long", stop_loss_pips=0)

    def test_negative_sl_rejected(self):
        with pytest.raises(ValueError, match="stop_loss_pips"):
            Signal(direction="long", stop_loss_pips=-10)

    def test_zero_tp_rejected(self):
        with pytest.raises(ValueError, match="take_profit_pips"):
            Signal(direction="long", stop_loss_pips=20, take_profit_pips=0)

    def test_negative_units_rejected(self):
        """Units are unsigned in Signal — sign comes from direction."""
        with pytest.raises(ValueError, match="units"):
            Signal(direction="long", stop_loss_pips=20, units=-100)

    def test_reason_defaults_to_empty(self):
        s = Signal(direction="long", stop_loss_pips=20)
        assert s.reason == ""


# --- Strategy ABC enforcement ---

class TestStrategyABC:
    def test_cannot_instantiate_abstract_class(self):
        with pytest.raises(TypeError):
            Strategy()

    def test_subclass_missing_methods_cannot_instantiate(self):
        class Broken(Strategy):
            pass
        with pytest.raises(TypeError):
            Broken()

    def test_default_history_size_is_200(self):
        """Default supports EMA(200) windows without subclass overrides."""
        s = FixedSignalStrategy()
        assert s.history_size == 200

    def test_subclass_can_override_history_size(self):
        class ShortHistory(Strategy):
            @property
            def instrument(self):
                return "EUR_USD"
            @property
            def granularity(self):
                return "M5"
            @property
            def history_size(self):
                return 14  # e.g., just enough for RSI(14)
            def on_candle_close(self, candle, history):
                return None
        assert ShortHistory().history_size == 14


# --- FixedSignalStrategy behavior ---

class TestFixedStrategyEmit:
    def test_emits_signal_on_every_candle(self):
        strategy = FixedSignalStrategy()
        candle = {"instrument": "EUR_USD", "granularity": "M1", "close": Decimal("1.10000")}
        signal = strategy.on_candle_close(candle, [])
        assert signal is not None
        assert signal.direction == "long"

    def test_respects_constructor_overrides(self):
        strategy = FixedSignalStrategy(
            instrument="GBP_USD", granularity="M15", direction="short"
        )
        assert strategy.instrument == "GBP_USD"
        assert strategy.granularity == "M15"
        signal = strategy.on_candle_close({}, [])
        assert signal.direction == "short"
