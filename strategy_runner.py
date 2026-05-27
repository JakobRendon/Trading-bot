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
from decimal import Decimal

from oanda_api import OandaAPIError, OandaOrderRejected
from risk import position_size


# Minimal stdlib logging — Phase 4 will replace with structured logging
# and file rotation. For now, strategy events go to stdout via root logger.
logger = logging.getLogger(__name__)


class StrategyRunner:
    def __init__(
        self,
        api,
        guard,
        strategy,
        paper=True,
        default_risk_pct=1.0,
        account_currency="USD",
    ):
        """
        api: OandaAPI instance (used for orders and quote-to-account rate lookups)
        guard: FTMORiskGuard instance — used in both modes
        strategy: a Strategy subclass instance
        paper: if True, log signals without placing orders
        default_risk_pct: % of NAV to risk per trade when Signal.units is None
        account_currency: currency of the OANDA account — required for accurate
            position sizing on cross-currency pairs (e.g., EUR_GBP on a USD
            account). The runner fetches a live rate from OANDA at signal time
            when quote != account.
        """
        self.api = api
        self.guard = guard
        self.strategy = strategy
        self.paper = paper
        self.default_risk_pct = default_risk_pct
        self.account_currency = account_currency
        # Rolling per-(instrument, granularity) history. Trimmed to history_size.
        self._history = {}
        # Trade activity recorded for visibility in paper mode + tests
        self.activity = []

    def _quote_to_account_rate(self, instrument):
        """Return the live rate to convert this instrument's quote currency
        to the account currency, or None if quote already matches account.

        Tries the direct pair (quote_account) first, then the inverted pair
        (account_quote). Raises OandaAPIError if neither lookup succeeds.
        """
        if "_" not in instrument:
            raise ValueError(f"Unrecognized instrument format: {instrument}")
        quote = instrument.split("_")[1]
        if quote == self.account_currency:
            return None

        def _mid(pair):
            data = self.api.get_price(pair)
            prices = data.get("prices", [])
            if not prices:
                return None
            bid = Decimal(prices[0]["bids"][0]["price"])
            ask = Decimal(prices[0]["asks"][0]["price"])
            return (bid + ask) / Decimal(2)

        direct = f"{quote}_{self.account_currency}"
        try:
            mid = _mid(direct)
            if mid is not None:
                return mid
        except OandaAPIError:
            pass

        inverted = f"{self.account_currency}_{quote}"
        mid = _mid(inverted)
        if mid is None or mid <= 0:
            raise OandaAPIError(0, f"No rate available for {quote}->{self.account_currency}")
        return Decimal(1) / mid

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
        # Per-strategy "single open position" guard. Agent A flagged that
        # MeanReversion can stack entries on consecutive candles when BB/RSI
        # stay extreme — the global risk guard's 180-position cap is far
        # above what any one strategy should pile up.
        if self.guard.open_position_count() > 0:
            event = {
                "type": "blocked",
                "signal": signal,
                "reason": "position already open",
                "triggered_at": triggering_candle.get("start_time"),
            }
            self.activity.append(event)
            logger.info("Signal blocked: position already open (%s)", signal.reason)
            return

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
            self.strategy.on_trade_filled(signal, triggering_candle)
            return

        # Live execution
        units = signal.units
        if units is None:
            nav = float(self.guard.summary()["current_nav"])
            try:
                rate = self._quote_to_account_rate(self.strategy.instrument)
                units = position_size(
                    nav,
                    self.default_risk_pct,
                    signal.stop_loss_pips,
                    self.strategy.instrument,
                    account_currency=self.account_currency,
                    quote_to_account_rate=rate,
                )
            except (OandaAPIError, ValueError) as e:
                event = {"type": "error", "signal": signal, "error": str(e)}
                self.activity.append(event)
                logger.error("Position sizing failed for %s: %s",
                             self.strategy.instrument, e)
                return
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
            self.strategy.on_trade_filled(signal, triggering_candle)
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
