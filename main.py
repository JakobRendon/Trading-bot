import json
import threading
from datetime import datetime, timezone
import config
from oanda_api import OandaAPI, OandaAPIError
from oanda_stream import OandaStream
from candle_aggregator import CandleAggregator

# Use the first configured instrument for single-instrument menu actions.
# Streaming menu actions use the full config.INSTRUMENTS list.
INSTRUMENT = config.INSTRUMENTS[0]

api = OandaAPI(config.API_TOKEN, config.ACCOUNT_ID, config.BASE_URL)


def print_json(data):
    print(json.dumps(data, indent=2))


def safe(fn):
    """Wrap a menu action to print OANDA errors instead of crashing the menu."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
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


@safe
def market_order():
    direction = input("  Buy or Sell? (buy/sell): ").strip().lower()
    units = input("  Units (e.g. 100): ").strip()
    if not units.isdigit() or int(units) == 0:
        print("  Invalid units (must be a positive integer).")
        return
    units = int(units)
    if direction == "sell":
        units = -units
    elif direction != "buy":
        print("  Invalid direction.")
        return
    print(f"  Placing {'BUY' if units > 0 else 'SELL'} order for {abs(units)} units of {INSTRUMENT}...")
    data = api.place_market_order(INSTRUMENT, units)
    fill = data.get("orderFillTransaction", {})
    if fill:
        print(f"  Filled at: {fill.get('price')}  P/L: {fill.get('pl')}")
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
10. Exit
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
        if choice == "10":
            break
        action = ACTIONS.get(choice)
        if action:
            action()
        else:
            print("  Invalid option.")


if __name__ == "__main__":
    main()
