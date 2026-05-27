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
- Spread modeled as a fixed pip count per round-trip (entry pays half-spread,
  exit pays half-spread). Real-world spreads widen during news/illiquidity —
  pass a conservative `spread_pips` if backtesting that risk.
- SL gap-through is modeled: if a candle opens past the SL, the fill is at
  the candle's open (worse than the SL price). TP fills at the limit price
  always (OANDA take-profits are limit orders, not stops).
- No SL slippage beyond the gap-through case
- Uses risk.position_size() with explicit account_currency / rate
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

from risk import pip_size, position_size


FTMO_TIMEZONE = ZoneInfo("Europe/Prague")

# FTMO 2026 hard limits by challenge type. Used by Backtester to flag
# equity-curve points that would have tripped the challenge regardless of
# whether the strategy was net-profitable.
_FTMO_LIMITS = {
    "2-step": {"daily_loss_pct": Decimal("5"), "total_drawdown_pct": Decimal("10")},
    "1-step": {"daily_loss_pct": Decimal("3"), "total_drawdown_pct": Decimal("10")},
}


def _ftmo_date(epoch_seconds):
    """UTC epoch → date in FTMO's reset timezone (Europe/Prague)."""
    return datetime.fromtimestamp(epoch_seconds, tz=FTMO_TIMEZONE).date()


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
class FTMOViolation:
    """A point on the equity curve that would have failed FTMO.

    Recorded by Backtester when daily_loss or total_drawdown crosses the
    challenge's hard limit. Note: 1-Step's trailing drawdown is NOT modeled
    yet — total_drawdown is computed against starting_balance (matches the
    2-Step static rule). Add trailing logic before backtesting against a
    1-Step Challenge — see B2 in Analysis_Review_2026-05-27.md.
    """
    rule: str          # "daily_loss" or "total_drawdown"
    pct: Decimal       # how far the rule was breached
    limit: Decimal     # the FTMO hard limit (e.g. 5 for 2-step daily)
    timestamp: float   # epoch seconds of the offending candle


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
    ftmo_violations: List[FTMOViolation] = field(default_factory=list)
    ftmo_check_enabled: bool = False
    ftmo_challenge_type: str = "2-step"

    @property
    def ftmo_status(self) -> str:
        if not self.ftmo_check_enabled:
            return "not checked"
        return "PASS" if not self.ftmo_violations else "FAIL"

    @property
    def ftmo_daily_violations(self) -> List[FTMOViolation]:
        return [v for v in self.ftmo_violations if v.rule == "daily_loss"]

    @property
    def ftmo_drawdown_violations(self) -> List[FTMOViolation]:
        return [v for v in self.ftmo_violations if v.rule == "total_drawdown"]

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
        lines = [
            f"Trades: {self.num_trades} ({self.num_wins}W / {self.num_losses}L)",
            f"Win rate: {self.win_rate * 100:.1f}%",
            f"Profit factor: {pf_str}",
            f"Total P/L: {self.total_pl:.2f}",
            f"Final balance: {self.final_balance:.2f}",
            f"Max drawdown: {self.max_drawdown_pct:.2f}%",
        ]
        if self.ftmo_check_enabled:
            lines.append(f"FTMO ({self.ftmo_challenge_type}): {self.ftmo_status}")
            if self.ftmo_violations:
                daily = self.ftmo_daily_violations
                dd = self.ftmo_drawdown_violations
                if daily:
                    worst = max(daily, key=lambda v: v.pct)
                    when = datetime.fromtimestamp(
                        worst.timestamp, tz=FTMO_TIMEZONE
                    ).date().isoformat()
                    lines.append(
                        f"  Daily-loss breaches: {len(daily)} day(s); "
                        f"worst {worst.pct:.2f}% on {when} (limit {worst.limit}%)"
                    )
                if dd:
                    worst = max(dd, key=lambda v: v.pct)
                    when = datetime.fromtimestamp(
                        worst.timestamp, tz=FTMO_TIMEZONE
                    ).date().isoformat()
                    lines.append(
                        f"  Total-drawdown breach: worst {worst.pct:.2f}% on {when} "
                        f"(limit {worst.limit}%)"
                    )
        return "\n".join(lines)


class Backtester:
    def __init__(
        self,
        strategy,
        starting_balance=Decimal("25000"),
        risk_pct=1.0,
        account_currency="USD",
        quote_to_account_rate=None,
        spread_pips=0,
        challenge_type="2-step",
        ftmo_check=True,
    ):
        """
        quote_to_account_rate: when the instrument's quote currency differs
            from `account_currency` (e.g., USD_JPY on USD account → quote is JPY),
            this is required to size positions correctly and to convert P/L
            back into account currency. Pass an approximate historical rate;
            using a single fixed rate over a multi-month backtest is inexact
            but acceptable for Phase 5 validation. For matching quote/account
            currency (e.g., EUR_USD on USD), leave None.

        Raises ValueError if the instrument's quote currency differs from
        account_currency and no rate is provided — a Signal with explicit
        `units` would otherwise skip position_size's validation and silently
        produce quote-currency P/L (a bug, not a feature).
        """
        instrument = strategy.instrument
        if "_" in instrument:
            quote_currency = instrument.split("_")[1]
            if quote_currency != account_currency and quote_to_account_rate is None:
                raise ValueError(
                    f"Backtester for {instrument} on {account_currency} account "
                    f"requires quote_to_account_rate (rate from {quote_currency} "
                    f"to {account_currency}). Pass an explicit value to avoid "
                    f"silent quote-currency P/L on explicit-units signals."
                )

        self.strategy = strategy
        self.starting_balance = Decimal(str(starting_balance))
        self.risk_pct = risk_pct
        self.account_currency = account_currency
        self.quote_to_account_rate = quote_to_account_rate
        self.spread_pips = Decimal(str(spread_pips))
        # half_spread expressed in price units (e.g. 0.0001 for non-JPY pairs)
        self._half_spread = self.spread_pips * pip_size(strategy.instrument) / Decimal(2)

        if challenge_type not in _FTMO_LIMITS:
            raise ValueError(f"challenge_type must be one of {list(_FTMO_LIMITS)}")
        self.challenge_type = challenge_type
        self.ftmo_check = ftmo_check
        self._daily_loss_limit_pct = _FTMO_LIMITS[challenge_type]["daily_loss_pct"]
        self._total_drawdown_limit_pct = _FTMO_LIMITS[challenge_type]["total_drawdown_pct"]

    def run(self, candles) -> BacktestResult:
        """Replay candles chronologically, simulating fills and exits."""
        balance = self.starting_balance
        trades: List[SimulatedTrade] = []
        open_trade: Optional[SimulatedTrade] = None
        history: List[dict] = []
        equity_curve: List[Decimal] = [balance]

        # FTMO compliance tracking.
        # daily_start_balance: balance at 00:00 Europe/Prague that day.
        # daily_violated_dates: only one daily-loss violation per Prague date.
        # total_drawdown_violated: only one total-drawdown violation per run.
        ftmo_violations: List[FTMOViolation] = []
        daily_start_balance = balance
        daily_start_date = None
        daily_violated_dates = set()
        total_drawdown_violated = False

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

            # 2. Strategy evaluates EVERY candle (matches StrategyRunner
            #    post-A5: the strategy sees every candle so stateful
            #    indicators stay continuous; new signals are only acted on
            #    when no position is open).
            signal = self.strategy.on_candle_close(candle, list(history))
            if open_trade is None and signal is not None:
                new_trade = self._open_trade(signal, candle, balance)
                if new_trade is not None:
                    trades.append(new_trade)
                    open_trade = new_trade
                    self.strategy.on_trade_filled(signal, candle)

            # 3. Maintain rolling history (excluding the just-evaluated candle
            #    on the first pass; same shape as StrategyRunner).
            history.append(candle)
            excess = len(history) - self.strategy.history_size
            if excess > 0:
                del history[:excess]

            # 4. Record equity for drawdown tracking.
            #    For open trades, use mark-to-market on candle close, using
            #    the exit-side price (bid for long, ask for short) so the
            #    mark already includes the cost of closing now.
            if open_trade is not None:
                mid_close = candle["close"]
                if open_trade.is_long:
                    mtm_price = mid_close - self._half_spread
                else:
                    mtm_price = mid_close + self._half_spread
                mtm = self._mark_to_market(open_trade, mtm_price)
                equity_curve.append(balance + mtm)
            else:
                equity_curve.append(balance)

            # 5. FTMO compliance check on the candle's equity mark.
            if self.ftmo_check:
                prague_date = _ftmo_date(candle["start_time"])
                if daily_start_date is None or prague_date != daily_start_date:
                    daily_start_balance = balance
                    daily_start_date = prague_date
                equity = equity_curve[-1]
                # Daily loss vs the anchor balance at 00:00 CE(S)T
                if (
                    daily_start_balance > 0
                    and prague_date not in daily_violated_dates
                ):
                    loss = daily_start_balance - equity
                    if loss > 0:
                        pct = loss / daily_start_balance * Decimal(100)
                        if pct >= self._daily_loss_limit_pct:
                            ftmo_violations.append(FTMOViolation(
                                rule="daily_loss",
                                pct=pct,
                                limit=self._daily_loss_limit_pct,
                                timestamp=candle["start_time"],
                            ))
                            daily_violated_dates.add(prague_date)
                # Total drawdown vs starting_balance (static — matches 2-Step).
                # 1-Step trailing drawdown not yet modeled; see B2 in
                # Analysis_Review_2026-05-27.md.
                if (
                    not total_drawdown_violated
                    and self.starting_balance > 0
                ):
                    dd_loss = self.starting_balance - equity
                    if dd_loss > 0:
                        pct = dd_loss / self.starting_balance * Decimal(100)
                        if pct >= self._total_drawdown_limit_pct:
                            ftmo_violations.append(FTMOViolation(
                                rule="total_drawdown",
                                pct=pct,
                                limit=self._total_drawdown_limit_pct,
                                timestamp=candle["start_time"],
                            ))
                            total_drawdown_violated = True

        # Close any trade still open at the end of the data. Exit at the
        # appropriate bid/ask side so the spread cost on this final trade
        # is consistent with closed trades.
        if open_trade is not None and candles:
            last = candles[-1]
            mid_last = last["close"]
            if open_trade.is_long:
                exit_price = mid_last - self._half_spread
            else:
                exit_price = mid_last + self._half_spread
            open_trade.exit_time = last["start_time"]
            open_trade.exit_price = exit_price
            open_trade.exit_reason = "end"
            open_trade.pl = self._compute_pl(open_trade)
            balance += open_trade.pl
            equity_curve[-1] = balance

        return BacktestResult(
            trades=trades,
            starting_balance=self.starting_balance,
            final_balance=balance,
            equity_curve=equity_curve,
            ftmo_violations=ftmo_violations,
            ftmo_check_enabled=self.ftmo_check,
            ftmo_challenge_type=self.challenge_type,
        )

    def _open_trade(self, signal, candle, balance) -> Optional[SimulatedTrade]:
        # Candles are mid prices. Long fills at ask = mid + half_spread;
        # short fills at bid = mid - half_spread. SL/TP are stored in the
        # currency price (bid for long exits, ask for short exits) — both
        # are derived from the fill price.
        mid_close = candle["close"]
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
                    quote_to_account_rate=self.quote_to_account_rate,
                )
            except ValueError:
                return None  # e.g., cross-currency pair needing explicit rate
        if units <= 0:
            return None

        if signal.direction == "long":
            entry_price = mid_close + self._half_spread
            sl_price = entry_price - signal.stop_loss_pips * ps
            tp_price = (
                entry_price + signal.take_profit_pips * ps
                if signal.take_profit_pips else None
            )
        else:
            entry_price = mid_close - self._half_spread
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

        Models bid/ask via half-spread shifts of the mid OHLC:
          long exits at bid = mid - half_spread
          short exits at ask = mid + half_spread

        Gap-through SL: if the candle opens past the stop (in bid for long,
        ask for short), OANDA fills at market — the candle open price —
        which is worse than the stored sl_price. Take-profit fills at the
        limit price always (OANDA TPs are limit orders).

        If both SL and TP fall within the same candle, conservatively
        assumes SL fired first (worst-case for the strategy).
        """
        open_p = candle["open"]
        high = candle["high"]
        low = candle["low"]
        hs = self._half_spread
        if trade.is_long:
            # bid prices for the exit side
            bid_open = open_p - hs
            bid_low = low - hs
            bid_high = high - hs
            # SL: gap-through fills at the gap (bid_open <= sl_price)
            if bid_open <= trade.sl_price:
                return {"price": bid_open, "reason": "sl"}
            if bid_low <= trade.sl_price:
                return {"price": trade.sl_price, "reason": "sl"}
            if trade.tp_price is not None and bid_high >= trade.tp_price:
                return {"price": trade.tp_price, "reason": "tp"}
        else:  # short
            ask_open = open_p + hs
            ask_low = low + hs
            ask_high = high + hs
            if ask_open >= trade.sl_price:
                return {"price": ask_open, "reason": "sl"}
            if ask_high >= trade.sl_price:
                return {"price": trade.sl_price, "reason": "sl"}
            if trade.tp_price is not None and ask_low <= trade.tp_price:
                return {"price": trade.tp_price, "reason": "tp"}
        return None

    def _compute_pl(self, trade) -> Decimal:
        """P/L in account currency.

        Raw P/L is in the instrument's quote currency. When quote != account
        currency (e.g., USD_JPY on USD account), we multiply by
        `quote_to_account_rate` to convert. Single-rate approximation across
        the backtest window — exact daily rates are a Phase 6 enhancement.
        """
        diff = trade.exit_price - trade.entry_price
        if not trade.is_long:
            diff = -diff
        pl_quote = diff * Decimal(trade.units)
        if self.quote_to_account_rate is not None:
            return pl_quote * Decimal(str(self.quote_to_account_rate))
        return pl_quote

    def _mark_to_market(self, trade, current_price) -> Decimal:
        """Unrealized P/L at current_price (for equity curve tracking).

        Same quote→account conversion as _compute_pl.
        """
        diff = current_price - trade.entry_price
        if not trade.is_long:
            diff = -diff
        pl_quote = diff * Decimal(trade.units)
        if self.quote_to_account_rate is not None:
            return pl_quote * Decimal(str(self.quote_to_account_rate))
        return pl_quote


# --- Walk-forward analysis ---

@dataclass
class WindowResult:
    """One test window's contribution to a walk-forward run."""
    start_time: float
    end_time: float
    result: BacktestResult


@dataclass
class WalkForwardResult:
    """Aggregate of per-window backtest results.

    Walk-forward is the plan's required validation pattern: split history
    into multiple test windows, run the strategy on each independently, and
    look for consistency. A strategy that wins on one window but loses on
    another isn't robust — fixed-parameter walk-forward surfaces this even
    without parameter optimization.
    """
    windows: List[WindowResult]

    @property
    def total_trades(self) -> int:
        return sum(w.result.num_trades for w in self.windows)

    @property
    def total_pl(self) -> Decimal:
        return sum((w.result.total_pl for w in self.windows), Decimal(0))

    @property
    def aggregate_win_rate(self) -> Decimal:
        wins = sum(w.result.num_wins for w in self.windows)
        total = self.total_trades
        if total == 0:
            return Decimal(0)
        return Decimal(wins) / Decimal(total)

    @property
    def aggregate_profit_factor(self) -> Optional[Decimal]:
        gross_profit = Decimal(0)
        gross_loss = Decimal(0)
        for w in self.windows:
            for t in w.result.closed_trades:
                if t.pl is None:
                    continue
                if t.pl > 0:
                    gross_profit += t.pl
                elif t.pl < 0:
                    gross_loss += abs(t.pl)
        if gross_loss == 0:
            return None
        return gross_profit / gross_loss

    @property
    def profitable_windows_pct(self) -> Decimal:
        if not self.windows:
            return Decimal(0)
        winners = sum(1 for w in self.windows if w.result.total_pl > 0)
        return Decimal(winners) / Decimal(len(self.windows)) * Decimal(100)

    @property
    def worst_window_pl(self) -> Decimal:
        if not self.windows:
            return Decimal(0)
        return min(w.result.total_pl for w in self.windows)

    @property
    def best_window_pl(self) -> Decimal:
        if not self.windows:
            return Decimal(0)
        return max(w.result.total_pl for w in self.windows)

    def summary(self) -> str:
        pf = self.aggregate_profit_factor
        pf_str = f"{pf:.2f}" if pf is not None else "N/A"
        lines = [
            f"Windows: {len(self.windows)}",
            f"Profitable windows: {self.profitable_windows_pct:.0f}%",
            f"Total trades: {self.total_trades}",
            f"Aggregate win rate: {self.aggregate_win_rate * 100:.1f}%",
            f"Aggregate profit factor: {pf_str}",
            f"Total P/L (sum of windows): {self.total_pl:.2f}",
            f"Best window: {self.best_window_pl:.2f}",
            f"Worst window: {self.worst_window_pl:.2f}",
        ]
        # FTMO compliance across windows — only meaningful if check was enabled.
        if self.windows and self.windows[0].result.ftmo_check_enabled:
            passing = sum(1 for w in self.windows if w.result.ftmo_status == "PASS")
            lines.append(
                f"FTMO-passing windows: {passing}/{len(self.windows)} "
                f"({self.windows[0].result.ftmo_challenge_type})"
            )
        return "\n".join(lines)


class WalkForwardAnalyzer:
    """Run a strategy across rolling test windows.

    For each window, builds a fresh strategy via `strategy_factory()` and
    runs an isolated backtest. Trades opened during the window's warmup
    period are filtered out so they don't contaminate the window's metrics.

    This is the simple (no-optimization) form of walk-forward. The full
    Phase 6 form adds a `train` period where parameters are optimized
    against a separate `test` period.
    """

    def __init__(
        self,
        strategy_factory: Callable,
        starting_balance=Decimal("25000"),
        risk_pct=1.0,
        account_currency="USD",
        quote_to_account_rate=None,
        spread_pips=0,
        challenge_type="2-step",
        ftmo_check=True,
    ):
        self.strategy_factory = strategy_factory
        self.starting_balance = Decimal(str(starting_balance))
        self.risk_pct = risk_pct
        self.account_currency = account_currency
        self.quote_to_account_rate = quote_to_account_rate
        self.spread_pips = spread_pips
        self.challenge_type = challenge_type
        self.ftmo_check = ftmo_check

    def run(
        self,
        candles,
        window_size: int,
        step: Optional[int] = None,
        warmup: int = 64,
    ) -> WalkForwardResult:
        """Run walk-forward on the given candles.

        window_size: candles per test window
        step: candles between window starts (defaults to window_size = non-overlapping)
        warmup: prior candles given to each window's strategy for context

        Trades that open during the warmup portion of a chunk are filtered
        out of the window's result so they don't double-count.
        """
        if step is None:
            step = window_size
        if not candles or len(candles) < warmup + window_size:
            return WalkForwardResult(windows=[])

        windows: List[WindowResult] = []
        start_idx = warmup

        while start_idx + window_size <= len(candles):
            end_idx = start_idx + window_size
            chunk_start = max(0, start_idx - warmup)
            chunk = candles[chunk_start:end_idx]

            strategy = self.strategy_factory()
            backtester = Backtester(
                strategy=strategy,
                starting_balance=self.starting_balance,
                risk_pct=self.risk_pct,
                account_currency=self.account_currency,
                quote_to_account_rate=self.quote_to_account_rate,
                spread_pips=self.spread_pips,
                challenge_type=self.challenge_type,
                ftmo_check=self.ftmo_check,
            )
            raw_result = backtester.run(chunk)

            window_start_time = candles[start_idx]["start_time"]
            window_end_time = candles[end_idx - 1]["start_time"]

            # Filter to trades that opened within the test window (skip warmup).
            window_trades = [
                t for t in raw_result.trades
                if window_start_time <= t.entry_time <= window_end_time
            ]
            window_pl = sum(
                (t.pl for t in window_trades if t.pl is not None),
                Decimal(0),
            )
            # Filter FTMO violations to those that occurred within the test
            # window — warmup-period violations aren't representative.
            window_violations = [
                v for v in raw_result.ftmo_violations
                if window_start_time <= v.timestamp <= window_end_time
            ]
            # Equity curve derived from the filtered trades: balance at each
            # close. Coarser than the per-candle curve (no intra-trade MTM)
            # but enough for max_drawdown_pct to surface inter-trade troughs.
            window_equity: List[Decimal] = [self.starting_balance]
            running = self.starting_balance
            for t in window_trades:
                if t.pl is not None:
                    running += t.pl
                window_equity.append(running)
            window_result = BacktestResult(
                trades=window_trades,
                starting_balance=self.starting_balance,
                final_balance=self.starting_balance + window_pl,
                equity_curve=window_equity,
                ftmo_violations=window_violations,
                ftmo_check_enabled=self.ftmo_check,
                ftmo_challenge_type=self.challenge_type,
            )

            windows.append(WindowResult(
                start_time=window_start_time,
                end_time=window_end_time,
                result=window_result,
            ))

            start_idx += step

        return WalkForwardResult(windows=windows)
