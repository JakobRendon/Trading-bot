"""
Strategy framework for the trading bot.

Strategies subclass `Strategy` and implement `on_candle_close()`. The runner
(strategy_runner.py) wires them to live market data, gates signals through
the risk guard, and executes (or paper-trades) the resulting orders.

Strategies receive the closed candle plus a history window. They return a
`Signal` to indicate a trade, or `None` for no action. Strategies do NOT
handle execution, risk, or position sizing — those concerns live elsewhere
so the same strategy can be backtested without modification.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class Signal:
    """A directional trade signal emitted by a strategy.

    The runner translates this into an order, sizing it through risk.py and
    gating it through risk_guard.py before placement.
    """

    direction: Literal["long", "short"]
    stop_loss_pips: int
    take_profit_pips: Optional[int] = None
    # Optional explicit unit count. If None, the runner sizes from account
    # risk % using risk.position_size().
    units: Optional[int] = None
    # Free-form annotation for logging / audit.
    reason: str = ""

    def __post_init__(self):
        if self.direction not in ("long", "short"):
            raise ValueError(f"direction must be 'long' or 'short', got {self.direction!r}")
        if self.stop_loss_pips <= 0:
            raise ValueError(f"stop_loss_pips must be positive, got {self.stop_loss_pips}")
        if self.take_profit_pips is not None and self.take_profit_pips <= 0:
            raise ValueError(f"take_profit_pips must be positive, got {self.take_profit_pips}")
        if self.units is not None and self.units <= 0:
            raise ValueError(f"units must be positive (sign comes from direction), got {self.units}")


class Strategy(ABC):
    """Abstract base class for trading strategies.

    Subclasses must implement:
    - `instrument` property: the OANDA instrument to trade (e.g., "EUR_USD")
    - `granularity` property: the candle granularity to evaluate (e.g., "M15")
    - `on_candle_close(candle, history)`: evaluate the closed candle and
      return a Signal or None

    Strategies may carry internal state (last signal time, position tracking,
    indicator memoization). The framework does not enforce statelessness —
    but stateful strategies are harder to backtest deterministically, so
    prefer pure computation from the candle window where possible.
    """

    @property
    @abstractmethod
    def instrument(self) -> str:
        """The instrument this strategy trades (e.g., 'EUR_USD')."""

    @property
    @abstractmethod
    def granularity(self) -> str:
        """The candle granularity this strategy evaluates (e.g., 'M15')."""

    @property
    def history_size(self) -> int:
        """How many prior candles to retain. Override for indicators needing more.

        Defaults to 200 — enough for EMA(200). Strategies using only short
        windows can override to a smaller value to save memory; backtests
        with millions of candles benefit from tight bounds.
        """
        return 200

    @abstractmethod
    def on_candle_close(self, candle, history) -> Optional[Signal]:
        """Evaluate the just-closed candle and return a Signal or None.

        candle: dict with keys instrument, granularity, start_time (epoch
                seconds), open, high, low, close, volume. Decimal-typed OHLC.
        history: list of prior closed candles in chronological order
                 (oldest first, most recent last). Excludes `candle` itself.
                 May be shorter than history_size early in the session.

        Return None to do nothing. Return a Signal to request a trade.
        """

    def on_trade_filled(self, signal, candle):
        """Called by the runner/backtester after a signal results in a fill.

        Default is no-op. Strategies with one-per-day or post-fill state
        (e.g., LondonBreakout's _last_trade_date) override this to commit
        that state only after the order actually executed — so a guard
        rejection or FOK cancel doesn't silently lock the strategy out.
        """


class FixedSignalStrategy(Strategy):
    """Wiring-test strategy: emits a fixed signal on every candle close.

    Has zero trading edge. Use only to verify the runner/stream/aggregator
    plumbing works end-to-end. Do NOT trade with this live.
    """

    def __init__(self, instrument="EUR_USD", granularity="M1", direction="long"):
        self._instrument = instrument
        self._granularity = granularity
        self._direction = direction

    @property
    def instrument(self) -> str:
        return self._instrument

    @property
    def granularity(self) -> str:
        return self._granularity

    def on_candle_close(self, candle, history):
        return Signal(
            direction=self._direction,
            stop_loss_pips=20,
            take_profit_pips=40,
            reason="test-strategy",
        )
