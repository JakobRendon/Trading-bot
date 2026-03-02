import json
import config
from oanda_api import OandaAPI

INSTRUMENT = "EUR_USD"

api = OandaAPI(config.API_TOKEN, config.ACCOUNT_ID, config.BASE_URL)


def print_json(data):
    print(json.dumps(data, indent=2))


def account_summary():
    data = api.get_account_summary()
    acct = data.get("account", {})
    print(f"  Balance:        {acct.get('balance')}")
    print(f"  Unrealized P/L: {acct.get('unrealizedPL')}")
    print(f"  NAV:            {acct.get('NAV')}")
    print(f"  Open Trades:    {acct.get('openTradeCount')}")
    print(f"  Margin Used:    {acct.get('marginUsed')}")


def candles():
    granularity = input("  Granularity (M1/M5/M15/H1/D) [M1]: ").strip() or "M1"
    count = input("  Number of candles [10]: ").strip() or "10"
    data = api.get_candles(INSTRUMENT, granularity, int(count))
    for candle in data.get("candles", []):
        mid = candle.get("mid", {})
        print(
            f"  {candle['time'][:19]}  "
            f"O:{mid.get('o')}  H:{mid.get('h')}  L:{mid.get('l')}  C:{mid.get('c')}  "
            f"Vol:{candle.get('volume')}"
        )


def current_price():
    data = api.get_price(INSTRUMENT)
    for price in data.get("prices", []):
        print(f"  {price['instrument']}  Bid: {price['bids'][0]['price']}  Ask: {price['asks'][0]['price']}")


def market_order():
    direction = input("  Buy or Sell? (buy/sell): ").strip().lower()
    units = input("  Units (e.g. 100): ").strip()
    if not units.isdigit():
        print("  Invalid units.")
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


def close_position():
    print(f"  Closing all {INSTRUMENT} positions...")
    data = api.close_position(INSTRUMENT)
    long_close = data.get("longOrderFillTransaction")
    short_close = data.get("shortOrderFillTransaction")
    if long_close:
        print(f"  Long closed — Units: {long_close.get('units')}  P/L: {long_close.get('pl')}")
    if short_close:
        print(f"  Short closed — Units: {short_close.get('units')}  P/L: {short_close.get('pl')}")
    if not long_close and not short_close:
        print_json(data)


MENU = """
--- OANDA Trading Bot ---
1. Account summary
2. EUR/USD candles
3. EUR/USD current price
4. Place market order (EUR/USD)
5. View open positions
6. Close EUR/USD position
7. Exit
"""

ACTIONS = {
    "1": account_summary,
    "2": candles,
    "3": current_price,
    "4": market_order,
    "5": open_positions,
    "6": close_position,
}


def main():
    if not config.API_TOKEN or not config.ACCOUNT_ID:
        print("Missing OANDA_API_TOKEN or OANDA_ACCOUNT_ID in .env file.")
        print("Copy .env.example to .env and fill in your credentials.")
        return

    print(f"Connected to: {config.BASE_URL}")
    while True:
        print(MENU)
        choice = input("Choose an option: ").strip()
        if choice == "7":
            break
        action = ACTIONS.get(choice)
        if action:
            action()
        else:
            print("  Invalid option.")


if __name__ == "__main__":
    main()
