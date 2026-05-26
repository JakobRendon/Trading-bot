"""
Minimal backtest engine.

Replays historical candles through a Strategy, simulates order fills, detects
SL/TP hits in subsequent candles, and produces trade-level + summary metrics.

Scope is deliberately limited to validate Phase 5 strategies before live
deployment. The full Phase 6 plan adds:
- Walk-forward harness (train/test rolling windows)
- Spread/slippage modeling
- FTMO compliance simulation during the backtest
- Candle caching to disk
- Multi-instrument portfolios

Current limitations:
- Single open position at a time (no scaling, no hedging)
- If SL and TP both fall within the same candle, conservatively assumes
  SL fired first (worst-case for the strategy)
- P/L calc assumes quote currency == account currency
  (correct for EUR_USD on USD; off by ~150x for USD_JPY on USD)
- No spread cost — fills happen at the candle's close price
- Uses risk.position_size() with default account_currency=USD
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from risk import pip_size, position_size


def normalize_oanda_candle(oanda_candle, instrument, granularity, price_key="mid"):
    """Convert an OANDA candle response to our internal candle format.

    OANDA format:
        {"time": "2024-01-01T00:00:00.000000000Z",
         "volume": 100,
         "mid": {"o": "1.10000", "h": "1.10100", "l": "1.09950", "c": "1.10050"}}
    Internal format:
        {"instrument", "granularity", "start_time" (epoch float),
         "open", "high", "low", "close" (Decimal), "volume" (int)}
    """
    prices = oanda_candle[price_key]
    # OANDA timestamps may have nanosecond precision (9 fractional digits) —
    # Python's fromisoformat only accepts microseconds, so trim.
    time_str = oanda_candle["time"]
    if "." in time_str:
        date_part, frac = time_str.split(".", 1)
        frac = frac.rstrip("Z")[:6]
        time_str = f"{date_part}.{frac}+00:00"
    else:
        time_str = time_str.rstrip("Z") + "+00:00"
    epoch = datetime.fromisoformat(time_str).timestamp()
    return {
        "instrument": instrument,
        "granularity": granularity,
        "start_time": epoch,
        "open": Decimal(prices["o"]),
        "high": Decimal(prices["h"]),
        "low": Decimal(prices["l"]),
        "close": Decimal(prices["c"]),
        "volume": int(oanda_candle.get("volume", 0)),
    }


@dataclass
class SimulatedTrade:
    direction: str  # "long" or "short"
    units: int
    entry_time: float  # epoch seconds
    entry_price: Decimal
    sl_price: Decimal
    tp_price: Optional[Decimal]  # None if signal had no TP
    exit_time: Optional[float] = None
    exit_price: Optional[Decimal] = None
    exit_reason: Optional[str] = None  # "sl", "tp", "end"
    pl: Optional[Decimal] = None
    signal_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def is_long(self) -> bool:
        return self.direction == "long"


@dataclass
class BacktestResult:
    trades: List[SimulatedTrade]
    starting_balance: Decimal
    final_balance: Decimal
    equity_curve: List[Decimal] = field(default_factory=list)

    @property
    def closed_trades(self):
        return [t for t in self.trades if not t.is_open]

    @property
    def total_pl(self) -> Decimal:
        return self.final_balance - self.starting_balance

    @property
    def num_trades(self) -> int:
        return len(self.closed_trades)

    @property
    def num_wins(self) -> int:
        return sum(1 for t in self.closed_trades if t.pl is not None and t.pl > 0)

    @property
    def num_losses(self) -> int:
        return sum(1 for t in self.closed_trades if t.pl is not None and t.pl < 0)

    @property
    def win_rate(self) -> Decimal:
        if not self.closed_trades:
            return Decimal(0)
        return Decimal(self.num_wins) / Decimal(len(self.closed_trades))

    @property
    def profit_factor(self) -> Optional[Decimal]:
        gross_profit = sum(
            (t.pl for t in self.closed_trades if t.pl and t.pl > 0),
            Decimal(0),
        )
        gross_loss = abs(sum(
            (t.pl for t in self.closed_trades if t.pl and t.pl < 0),
            Decimal(0),
        ))
        if gross_loss == 0:
            return None  # Undefined — all wins (or no losses)
        return gross_profit / gross_loss

    @property
    def max_drawdown_pct(self) -> Decimal:
        """Maximum peak-to-trough drawdown on the equity curve, as percent."""
        if not self.equity_curve:
            return Decimal(0)
        peak = self.equity_curve[0]
        max_dd = Decimal(0)
        for equity in self.equity_curve:
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (peak - equity) / peak * Decimal(100)
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    def summary(self) -> str:
        pf_str = f"{self.profit_factor:.2f}" if self.profit_factor is not None else "N/A"
        return (
            f"Trades: {self.num_trades} ({self.num_wins}W / {self.num_losses}L)\n"
            f"Win rate: {self.win_rate * 100:.1f}%\n"
            f"Profit factor: {pf_str}\n"
            f"Total P/L: {self.total_pl:.2f}\n"
            f"Final balance: {self.final_balance:.2f}\n"
            f"Max drawdown: {self.max_drawdown_pct:.2f}%"
        )


class Backtester:
    def __init__(
        self,
        strategy,
        starting_balance=Decimal("25000"),
        risk_pct=1.0,
        account_currency="USD",
    ):
        self.strategy = strategy
        self.starting_balance = Decimal(str(starting_balance))
        self.risk_pct = risk_pct
        self.account_currency = account_currency

    def run(self, candles) -> BacktestResult:
        """Replay candles chronologically, simulating fills and exits."""
        balance = self.starting_balance
        trades: List[SimulatedTrade] = []
        open_trade: Optional[SimulatedTrade] = None
        history: List[dict] = []
        equity_curve: List[Decimal] = [balance]

        for candle in candles:
            # 1. If a trade is open, check if SL or TP was hit this candle.
            if open_trade is not None:
                exit_info = self._check_exit(open_trade, candle)
                if exit_info:
                    open_trade.exit_time = candle["start_time"]
                    open_trade.exit_price = exit_info["price"]
                    open_trade.exit_reason = exit_info["reason"]
                    open_trade.pl = self._compute_pl(open_trade)
                    balance += open_trade.pl
                    open_trade = None

            # 2. Strategy evaluates the candle (only if no position is open).
            if open_trade is None:
                signal = self.strategy.on_candle_close(candle, list(history))
                if signal is not None:
                    new_trade = self._open_trade(signal, candle, balance)
                    if new_trade is not None:
                        trades.append(new_trade)
                        open_trade = new_trade

            # 3. Maintain rolling history (excluding the just-evaluated candle
            #    on the first pass; same shape as StrategyRunner).
            history.append(candle)
            excess = len(history) - self.strategy.history_size
            if excess > 0:
                del history[:excess]

            # 4. Record equity for drawdown tracking.
            #    For open trades, use mark-to-market on candle close.
            if open_trade is not None:
                mtm = self._mark_to_market(open_trade, candle["close"])
                equity_curve.append(balance + mtm)
            else:
                equity_curve.append(balance)

        # Close any trade still open at the end of the data.
        if open_trade is not None and candles:
            last = candles[-1]
            open_trade.exit_time = last["start_time"]
            open_trade.exit_price = last["close"]
            open_trade.exit_reason = "end"
            open_trade.pl = self._compute_pl(open_trade)
            balance += open_trade.pl
            equity_curve[-1] = balance

        return BacktestResult(
            trades=trades,
            starting_balance=self.starting_balance,
            final_balance=balance,
            equity_curve=equity_curve,
        )

    def _open_trade(self, signal, candle, balance) -> Optional[SimulatedTrade]:
        entry_price = candle["close"]
        ps = pip_size(self.strategy.instrument)
        units = signal.units
        if units is None:
            try:
                units = position_size(
                    balance,
                    self.risk_pct,
                    signal.stop_loss_pips,
                    self.strategy.instrument,
                    account_currency=self.account_currency,
                )
            except ValueError:
                return None  # e.g., cross-currency pair needing explicit rate
        if units <= 0:
            return None

        if signal.direction == "long":
            sl_price = entry_price - signal.stop_loss_pips * ps
            tp_price = (
                entry_price + signal.take_profit_pips * ps
                if signal.take_profit_pips else None
            )
        else:
            sl_price = entry_price + signal.stop_loss_pips * ps
            tp_price = (
                entry_price - signal.take_profit_pips * ps
                if signal.take_profit_pips else None
            )

        return SimulatedTrade(
            direction=signal.direction,
            units=units,
            entry_time=candle["start_time"],
            entry_price=entry_price,
            sl_price=sl_price,
            tp_price=tp_price,
            signal_reason=signal.reason,
        )

    def _check_exit(self, trade, candle) -> Optional[dict]:
        """Return {price, reason} if the trade hit SL or TP in this candle.

        If both fall within the candle's range, conservatively assumes SL
        fired first (worst-case for the strategy).
        """
        high = candle["high"]
        low = candle["low"]
        if trade.is_long:
            hit_sl = low <= trade.sl_price
            hit_tp = trade.tp_price is not None and high >= trade.tp_price
            if hit_sl:
                return {"price": trade.sl_price, "reason": "sl"}
            if hit_tp:
                return {"price": trade.tp_price, "reason": "tp"}
        else:  # short
            hit_sl = high >= trade.sl_price
            hit_tp = trade.tp_price is not None and low <= trade.tp_price
            if hit_sl:
                return {"price": trade.sl_price, "reason": "sl"}
            if hit_tp:
                return {"price": trade.tp_price, "reason": "tp"}
        return None

    def _compute_pl(self, trade) -> Decimal:
        """P/L in account currency.

        Assumes quote currency == account currency. For mismatched pairs
        (e.g., USD_JPY on USD account), the result is in JPY and needs
        conversion — deferred to full Phase 6.
        """
        diff = trade.exit_price - trade.entry_price
        if not trade.is_long:
            diff = -diff
        return diff * Decimal(trade.units)

    def _mark_to_market(self, trade, current_price) -> Decimal:
        """Unrealized P/L at current_price (for equity curve tracking)."""
        diff = current_price - trade.entry_price
        if not trade.is_long:
            diff = -diff
        return diff * Decimal(trade.units)
