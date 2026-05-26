"""
Strategy runner: wires a Strategy into the live data flow.

The runner subscribes to candle-close events from a CandleAggregator, calls
the strategy, gates the resulting Signal through the FTMORiskGuard, and
either executes the trade (live mode) or logs it (paper mode).

Wiring pattern:

    strategy = MyStrategy(instrument="EUR_USD", granularity="M15")
    runner = StrategyRunner(api, guard, strategy, paper=True)
    aggregator = CandleAggregator([strategy.granularity])
    aggregator.on_candle_close(runner.on_candle_close)
    stream.on_price(aggregator.on_tick)
    stream.start([strategy.instrument])

Paper mode (`paper=True`) is the default — strategies log would-be trades
without placing real orders. Flip to `paper=False` only after the strategy
has been validated.
"""

import logging
from oanda_api import OandaAPIError, OandaOrderRejected
from risk import position_size


# Minimal stdlib logging — Phase 4 will replace with structured logging
# and file rotation. For now, strategy events go to stdout via root logger.
logger = logging.getLogger(__name__)


class StrategyRunner:
    def __init__(self, api, guard, strategy, paper=True, default_risk_pct=1.0):
        """
        api: OandaAPI instance (only used when paper=False)
        guard: FTMORiskGuard instance — used in both modes
        strategy: a Strategy subclass instance
        paper: if True, log signals without placing orders
        default_risk_pct: % of NAV to risk per trade when Signal.units is None
        """
        self.api = api
        self.guard = guard
        self.strategy = strategy
        self.paper = paper
        self.default_risk_pct = default_risk_pct
        # Rolling per-(instrument, granularity) history. Trimmed to history_size.
        self._history = {}
        # Trade activity recorded for visibility in paper mode + tests
        self.activity = []

    def on_candle_close(self, granularity, candle):
        """Callback wired to CandleAggregator.on_candle_close().

        Filters to this strategy's instrument + granularity, maintains the
        rolling history window, invokes the strategy, then routes any signal.
        """
        if candle.get("instrument") != self.strategy.instrument:
            return
        if granularity != self.strategy.granularity:
            return

        key = (self.strategy.instrument, granularity)
        history = self._history.setdefault(key, [])

        # Strategy sees prior candles; the just-closed one is the first arg.
        try:
            signal = self.strategy.on_candle_close(candle, list(history))
        except Exception as e:
            logger.exception("Strategy %s raised on candle close: %s",
                             type(self.strategy).__name__, e)
            signal = None

        # Append after the strategy evaluation so the strategy doesn't see
        # itself in history.
        history.append(candle)
        excess = len(history) - self.strategy.history_size
        if excess > 0:
            del history[:excess]

        if signal is None:
            return

        self._handle_signal(signal, candle)

    def _handle_signal(self, signal, triggering_candle):
        allowed, reason = self.guard.can_open_position()
        if not allowed:
            event = {
                "type": "blocked",
                "signal": signal,
                "reason": reason,
                "triggered_at": triggering_candle.get("start_time"),
            }
            self.activity.append(event)
            logger.info("Signal blocked by risk guard: %s (%s)", reason, signal.reason)
            return

        if self.paper:
            event = {
                "type": "paper",
                "signal": signal,
                "triggered_at": triggering_candle.get("start_time"),
            }
            self.activity.append(event)
            logger.info(
                "PAPER %s %s SL:%s TP:%s reason:%s",
                signal.direction,
                self.strategy.instrument,
                signal.stop_loss_pips,
                signal.take_profit_pips,
                signal.reason,
            )
            return

        # Live execution
        units = signal.units
        if units is None:
            nav = float(self.guard.summary()["current_nav"])
            units = position_size(
                nav, self.default_risk_pct, signal.stop_loss_pips, self.strategy.instrument
            )
            if units == 0:
                logger.warning(
                    "Computed position size is 0 for %s — skipping signal",
                    self.strategy.instrument,
                )
                return
        if signal.direction == "short":
            units = -units

        try:
            response = self.api.place_market_order(
                self.strategy.instrument,
                units,
                stop_loss_pips=signal.stop_loss_pips,
                take_profit_pips=signal.take_profit_pips,
            )
        except (OandaAPIError, OandaOrderRejected, ValueError) as e:
            event = {"type": "error", "signal": signal, "error": str(e)}
            self.activity.append(event)
            logger.error("Order placement failed: %s", e)
            return

        if "orderFillTransaction" in response:
            self.guard.record_position_entry()
            fill = response["orderFillTransaction"]
            event = {
                "type": "filled",
                "signal": signal,
                "fill_price": fill.get("price"),
                "transaction_id": fill.get("id"),
            }
            self.activity.append(event)
            logger.info(
                "FILLED %s %s @ %s (txID %s)",
                signal.direction,
                self.strategy.instrument,
                fill.get("price"),
                fill.get("id"),
            )
        elif "orderCancelTransaction" in response:
            cancel = response["orderCancelTransaction"]
            event = {
                "type": "cancelled",
                "signal": signal,
                "reason": cancel.get("reason"),
            }
            self.activity.append(event)
            logger.info(
                "FOK CANCELLED (%s) for %s signal",
                cancel.get("reason"),
                signal.direction,
            )
        else:
            event = {"type": "unknown", "signal": signal, "response": response}
            self.activity.append(event)
            logger.warning("Unexpected order response shape: %s", response)
