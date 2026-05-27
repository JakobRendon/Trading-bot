import json
import threading
from datetime import datetime, timezone
from decimal import Decimal
import config
from oanda_api import OandaAPI, OandaAPIError, OandaOrderRejected
from oanda_stream import OandaStream
from candle_aggregator import CandleAggregator
from risk import position_size, validate_risk_reward
from risk_guard import FTMORiskGuard
from strategy import FixedSignalStrategy
from strategy_runner import StrategyRunner
from london_breakout import LondonBreakoutStrategy
from mean_reversion import MeanReversionStrategy
from backtest import Backtester, WalkForwardAnalyzer, normalize_oanda_candle

# Use the first configured instrument for single-instrument menu actions.
# Streaming menu actions use the full config.INSTRUMENTS list.
INSTRUMENT = config.INSTRUMENTS[0]

api = OandaAPI(config.API_TOKEN, config.ACCOUNT_ID, config.BASE_URL)

guard = FTMORiskGuard(
    api,
    state_path=config.RISK_STATE_PATH,
    challenge_start_balance=config.CHALLENGE_START_BALANCE,
    daily_loss_buffer_pct=config.DAILY_LOSS_BUFFER_PCT,
    total_drawdown_buffer_pct=config.TOTAL_DRAWDOWN_BUFFER_PCT,
    max_requests_per_day=config.MAX_REQUESTS_PER_DAY,
    max_position_entries_per_day=config.MAX_POSITION_ENTRIES_PER_DAY,
    max_simultaneous_positions=config.MAX_SIMULTANEOUS_POSITIONS,
)


def print_json(data):
    print(json.dumps(data, indent=2))


def safe(fn):
    """Wrap a menu action to print OANDA errors instead of crashing the menu."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OandaOrderRejected as e:
            print(f"  Order rejected: {e}")
        except OandaAPIError as e:
            print(f"  API error: {e}")
        except ValueError as e:
            print(f"  {e}")
    wrapper.__name__ = fn.__name__
    return wrapper


@safe
def account_summary():
    data = api.get_account_summary()
    acct = data.get("account", {})
    print(f"  Balance:        {acct.get('balance')}")
    print(f"  Unrealized P/L: {acct.get('unrealizedPL')}")
    print(f"  NAV:            {acct.get('NAV')}")
    print(f"  Open Trades:    {acct.get('openTradeCount')}")
    print(f"  Margin Used:    {acct.get('marginUsed')}")


@safe
def candles():
    granularity = input("  Granularity (M1/M5/M15/H1/H4/D) [M1]: ").strip() or "M1"
    count = input("  Number of candles [10]: ").strip() or "10"
    data = api.get_candles(INSTRUMENT, granularity, int(count))
    for candle in data.get("candles", []):
        mid = candle.get("mid", {})
        print(
            f"  {candle['time'][:19]}  "
            f"O:{mid.get('o')}  H:{mid.get('h')}  L:{mid.get('l')}  C:{mid.get('c')}  "
            f"Vol:{candle.get('volume')}"
        )


@safe
def current_price():
    data = api.get_price(INSTRUMENT)
    for price in data.get("prices", []):
        print(f"  {price['instrument']}  Bid: {price['bids'][0]['price']}  Ask: {price['asks'][0]['price']}")


def _prompt_int(prompt, default=None, min_value=None):
    raw = input(prompt).strip()
    if not raw and default is not None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    if min_value is not None and value < min_value:
        return None
    return value


def _prompt_float(prompt, default=None, min_value=None):
    raw = input(prompt).strip()
    if not raw and default is not None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return None
    if min_value is not None and value < min_value:
        return None
    return value


@safe
def market_order():
    allowed, reason = guard.can_open_position()
    if not allowed:
        print(f"  Risk guard blocks new positions: {reason}")
        return

    direction = input("  Buy or Sell? (buy/sell): ").strip().lower()
    if direction not in ("buy", "sell"):
        print("  Invalid direction.")
        return

    # Auto-fetch current NAV — no error-prone manual entry.
    nav = float(guard.summary()["current_nav"])
    print(f"  Current NAV: {nav}")

    use_sizing = input("  Auto-size from risk %? (y/n) [y]: ").strip().lower() or "y"
    if use_sizing == "y":
        risk_pct = _prompt_float("  Risk % per trade [1.0]: ", default=1.0, min_value=0.01)
        if risk_pct is None:
            print("  Invalid risk %.")
            return
        sl_pips = _prompt_int("  Stop-loss in pips [30]: ", default=30, min_value=1)
        if sl_pips is None:
            print("  Invalid SL pips.")
            return
        units = position_size(nav, risk_pct, sl_pips, INSTRUMENT)
        if units == 0:
            print("  Computed position size is 0 — increase risk % or reduce SL.")
            return
        print(f"  Computed size: {units} units")
    else:
        units = _prompt_int("  Units (e.g. 100): ", min_value=1)
        if units is None:
            print("  Invalid units.")
            return
        # SL is required by Phase 3 "no naked positions" rule
        sl_pips = _prompt_int("  Stop-loss in pips [30]: ", default=30, min_value=1)
        if sl_pips is None:
            print("  Invalid SL pips.")
            return

    tp_pips = _prompt_int("  Take-profit in pips (blank for none): ", default=0)
    tp_pips = tp_pips if tp_pips and tp_pips > 0 else None

    if direction == "sell":
        units = -units

    print(
        f"  Placing {'BUY' if units > 0 else 'SELL'} order for {abs(units)} units of {INSTRUMENT} "
        f"(SL: {sl_pips} pips, TP: {tp_pips or '-'} pips)..."
    )
    # place_market_order enforces SL-required and R:R rules internally
    data = api.place_market_order(
        INSTRUMENT, units, stop_loss_pips=sl_pips, take_profit_pips=tp_pips
    )
    if "orderFillTransaction" in data:
        fill = data["orderFillTransaction"]
        print(f"  Filled at: {fill.get('price')}  P/L: {fill.get('pl')}")
        guard.record_position_entry()
    elif "orderCancelTransaction" in data:
        cancel = data["orderCancelTransaction"]
        print(f"  Order cancelled (not filled): {cancel.get('reason')}")
    else:
        print_json(data)


@safe
def open_positions():
    data = api.get_open_positions()
    positions = data.get("positions", [])
    if not positions:
        print("  No open positions.")
        return
    for pos in positions:
        long_units = pos.get("long", {}).get("units", "0")
        short_units = pos.get("short", {}).get("units", "0")
        unrealized = pos.get("unrealizedPL", "0")
        print(f"  {pos['instrument']}  Long: {long_units}  Short: {short_units}  P/L: {unrealized}")


@safe
def close_position():
    print(f"  Closing all {INSTRUMENT} positions...")
    data = api.close_position(INSTRUMENT)
    if data.get("noPosition"):
        print(f"  No open position for {INSTRUMENT}.")
        return
    long_close = data.get("longOrderFillTransaction")
    short_close = data.get("shortOrderFillTransaction")
    if long_close:
        print(f"  Long closed — Units: {long_close.get('units')}  P/L: {long_close.get('pl')}")
    if short_close:
        print(f"  Short closed — Units: {short_close.get('units')}  P/L: {short_close.get('pl')}")
    if not long_close and not short_close:
        print_json(data)


@safe
def stream_prices():
    duration_input = input("  Stream for how many seconds? [10]: ").strip() or "10"
    try:
        duration = int(duration_input)
    except ValueError:
        print("  Invalid duration.")
        return
    stream = OandaStream(config.API_TOKEN, config.ACCOUNT_ID, config.BASE_URL)

    def on_price(tick):
        bids = tick.get("bids") or []
        asks = tick.get("asks") or []
        bid = bids[0]["price"] if bids else "-"
        ask = asks[0]["price"] if asks else "-"
        print(f"  {tick['time'][:19]}  {tick['instrument']}  Bid: {bid}  Ask: {ask}")

    stream.on_price(on_price)
    print(f"  Streaming {','.join(config.INSTRUMENTS)} for {duration}s (Ctrl+C to stop early)...")

    timer = threading.Timer(duration, stream.stop)
    timer.start()
    try:
        stream.start(config.INSTRUMENTS, max_reconnects=5)
    except KeyboardInterrupt:
        stream.stop()
    finally:
        timer.cancel()
    print("  Stream stopped.")


@safe
def stream_candles():
    duration_input = input("  Stream for how many seconds? [120]: ").strip() or "120"
    try:
        duration = int(duration_input)
    except ValueError:
        print("  Invalid duration.")
        return
    stream = OandaStream(config.API_TOKEN, config.ACCOUNT_ID, config.BASE_URL)
    aggregator = CandleAggregator(config.GRANULARITIES)

    def on_close(granularity, candle):
        ts = datetime.fromtimestamp(candle["start_time"], tz=timezone.utc).isoformat()
        print(
            f"  CLOSE {granularity}  {ts[:19]}  {candle['instrument']}  "
            f"O:{candle['open']:.5f}  H:{candle['high']:.5f}  "
            f"L:{candle['low']:.5f}  C:{candle['close']:.5f}  "
            f"Ticks:{candle['volume']}"
        )

    aggregator.on_candle_close(on_close)
    stream.on_price(aggregator.on_tick)
    print(
        f"  Streaming {','.join(config.INSTRUMENTS)} and aggregating "
        f"{','.join(config.GRANULARITIES)} candles for {duration}s..."
    )

    timer = threading.Timer(duration, stream.stop)
    timer.start()
    try:
        stream.start(config.INSTRUMENTS, max_reconnects=5)
    except KeyboardInterrupt:
        stream.stop()
    finally:
        timer.cancel()
    print("  Stream stopped.")


def _pick_strategy(instrument):
    """Prompt for a strategy choice and return (strategy_factory, granularity)."""
    print("  Strategies:")
    print("    1. FixedSignalStrategy (M1, wiring test — fires every candle)")
    print("    2. LondonBreakoutStrategy (M15, real — fires 08-10 UTC max once/day)")
    print("    3. MeanReversionStrategy (H1, BB + RSI extremes)")
    choice = input("  Pick [2]: ").strip() or "2"
    if choice == "1":
        return (lambda: FixedSignalStrategy(instrument=instrument, granularity="M1")), "M1"
    if choice == "3":
        return (lambda: MeanReversionStrategy(instrument=instrument, granularity="H1")), "H1"
    return (lambda: LondonBreakoutStrategy(instrument=instrument)), "M15"


@safe
def run_strategy():
    """Run a strategy in paper mode against the live stream."""
    strategy_factory, granularity = _pick_strategy(INSTRUMENT)
    duration_input = input("  Run for how many seconds? [120]: ").strip() or "120"
    try:
        duration = int(duration_input)
    except ValueError:
        print("  Invalid duration.")
        return

    strategy = strategy_factory()
    print(f"  Running {type(strategy).__name__} on {INSTRUMENT} ({granularity}) in PAPER mode.")

    runner = StrategyRunner(api, guard, strategy, paper=True)
    stream = OandaStream(config.API_TOKEN, config.ACCOUNT_ID, config.BASE_URL)
    aggregator = CandleAggregator([granularity])
    aggregator.on_candle_close(runner.on_candle_close)
    stream.on_price(aggregator.on_tick)

    print(f"  Streaming {INSTRUMENT} for {duration}s (Ctrl+C to stop)...")
    timer = threading.Timer(duration, stream.stop)
    timer.start()
    try:
        stream.start([INSTRUMENT], max_reconnects=5)
    except KeyboardInterrupt:
        stream.stop()
    finally:
        timer.cancel()

    print(f"  Recorded {len(runner.activity)} strategy events:")
    for event in runner.activity:
        if event["type"] == "paper":
            sig = event["signal"]
            print(f"    PAPER {sig.direction} SL:{sig.stop_loss_pips} TP:{sig.take_profit_pips} ({sig.reason})")
        elif event["type"] == "blocked":
            print(f"    BLOCKED ({event['reason']})")
        else:
            print(f"    {event['type'].upper()}")


@safe
def backtest_strategy():
    """Run a historical backtest of a chosen strategy on OANDA candles."""
    strategy_factory, granularity = _pick_strategy(INSTRUMENT)
    days_input = input("  How many days of history? [180]: ").strip() or "180"
    try:
        days = int(days_input)
    except ValueError:
        print("  Invalid days.")
        return
    starting_balance_input = input("  Starting balance [25000]: ").strip() or "25000"
    try:
        starting_balance = Decimal(starting_balance_input)
    except (ValueError, ArithmeticError):
        print("  Invalid balance.")
        return

    from datetime import datetime, timedelta, timezone
    to_dt = datetime.now(timezone.utc)
    from_dt = to_dt - timedelta(days=days)
    from_iso = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_iso = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"  Fetching {INSTRUMENT} {granularity} candles ({days} days)...")
    oanda_candles = api.get_candles_range(INSTRUMENT, granularity, from_iso, to_iso)
    print(f"  Got {len(oanda_candles)} candles.")
    if not oanda_candles:
        print("  No candles returned.")
        return

    candles = [normalize_oanda_candle(c, INSTRUMENT, granularity) for c in oanda_candles]
    strategy = strategy_factory()
    result = Backtester(strategy, starting_balance=starting_balance).run(candles)
    print(f"  Strategy: {type(strategy).__name__}")
    print()
    print(result.summary())


_CANDLES_PER_DAY = {"M1": 1440, "M5": 288, "M15": 96, "M30": 48, "H1": 24, "H4": 6, "D": 1}


@safe
def walk_forward_strategy():
    """Walk-forward analysis of a chosen strategy across rolling test windows."""
    strategy_factory, granularity = _pick_strategy(INSTRUMENT)
    days_input = input("  Total history (days)? [180]: ").strip() or "180"
    window_days_input = input("  Test window size (days)? [30]: ").strip() or "30"
    try:
        days = int(days_input)
        window_days = int(window_days_input)
    except ValueError:
        print("  Invalid input.")
        return

    from datetime import datetime, timedelta, timezone
    to_dt = datetime.now(timezone.utc)
    from_dt = to_dt - timedelta(days=days)
    from_iso = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    to_iso = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"  Fetching {INSTRUMENT} {granularity} candles ({days} days)...")
    oanda_candles = api.get_candles_range(INSTRUMENT, granularity, from_iso, to_iso)
    print(f"  Got {len(oanda_candles)} candles.")
    if not oanda_candles:
        return

    candles = [normalize_oanda_candle(c, INSTRUMENT, granularity) for c in oanda_candles]
    cpd = _CANDLES_PER_DAY.get(granularity, 96)
    window_candles = window_days * cpd

    analyzer = WalkForwardAnalyzer(
        strategy_factory=strategy_factory,
        starting_balance=Decimal("25000"),
        risk_pct=1.0,
    )
    result = analyzer.run(candles, window_size=window_candles, warmup=cpd)

    print(f"  Strategy: {type(strategy_factory()).__name__}")
    print()
    print(result.summary())
    print()
    print("Per-window detail:")
    from datetime import datetime as _dt, timezone as _tz
    for i, w in enumerate(result.windows, 1):
        start = _dt.fromtimestamp(w.start_time, tz=_tz.utc).date()
        end = _dt.fromtimestamp(w.end_time, tz=_tz.utc).date()
        r = w.result
        pf = f"{r.profit_factor:.2f}" if r.profit_factor else "N/A"
        print(
            f"  Window {i} ({start}..{end}): trades={r.num_trades:>2} "
            f"win={r.win_rate * 100:>4.0f}% PF={pf:>5} P/L={r.total_pl:>8.0f}"
        )


@safe
def risk_status():
    s = guard.summary()
    allowed, reason = guard.can_open_position()
    print(f"  Challenge start balance: {s['challenge_start_balance']}")
    print(f"  Daily start balance:     {s['daily_start_balance']}")
    print(f"  Current NAV:             {s['current_nav']}")
    print(f"  Daily P/L:               {s['daily_pl']}")
    print(f"  Daily loss %:            {s['daily_loss_pct']}%  (buffer {guard.daily_loss_buffer_pct}%)")
    print(f"  Total drawdown %:        {s['total_drawdown_pct']}%  (buffer {guard.total_drawdown_buffer_pct}%)")
    print(f"  Daily requests:          {s['daily_requests']} / {s['max_requests_per_day']}")
    print(f"  Daily entries:           {s['daily_entries']} / {s['max_entries_per_day']}")
    print(f"  Open positions:          {s['open_positions']} / {s['max_simultaneous']}")
    print(f"  Trading allowed:         {'YES' if allowed else f'NO ({reason})'}")


@safe
def transactions():
    count_input = input("  Show last N transactions [10]: ").strip() or "10"
    try:
        count = int(count_input)
    except ValueError:
        print("  Invalid count.")
        return
    data = api.get_transactions()
    txs = data.get("transactions", [])
    if not txs:
        print("  No transactions.")
        return
    recent = txs[-count:] if count > 0 else txs
    for tx in recent:
        instrument = tx.get("instrument", "-")
        units = tx.get("units", "-")
        price = tx.get("price", "-")
        pl = tx.get("pl", "-")
        print(
            f"  ID:{tx['id']}  {tx['time'][:19]}  {tx['type']:25s}  "
            f"{instrument}  Units:{units}  Price:{price}  P/L:{pl}"
        )


MENU = """
--- OANDA Trading Bot ---
1. Account summary
2. Get candles
3. Current price
4. Place market order
5. View open positions
6. Close position
7. View transaction history
8. Stream live prices
9. Stream + aggregate candles
10. Risk status
11. Run strategy (paper mode)
12. Backtest a strategy
13. Walk-forward a strategy
14. Exit
"""

ACTIONS = {
    "1": account_summary,
    "2": candles,
    "3": current_price,
    "4": market_order,
    "5": open_positions,
    "6": close_position,
    "7": transactions,
    "8": stream_prices,
    "9": stream_candles,
    "10": risk_status,
    "11": run_strategy,
    "12": backtest_strategy,
    "13": walk_forward_strategy,
}


def main():
    if not config.API_TOKEN or not config.ACCOUNT_ID:
        print("Missing OANDA_API_TOKEN or OANDA_ACCOUNT_ID in .env file.")
        print("Copy .env.example to .env and fill in your credentials.")
        return

    print(f"Connected to: {config.BASE_URL}")
    print(f"Instruments: {','.join(config.INSTRUMENTS)}")
    print(f"Granularities: {','.join(config.GRANULARITIES)}")
    while True:
        print(MENU)
        choice = input("Choose an option: ").strip()
        if choice == "14":
            break
        action = ACTIONS.get(choice)
        if action:
            action()
        else:
            print("  Invalid option.")


if __name__ == "__main__":
    main()
