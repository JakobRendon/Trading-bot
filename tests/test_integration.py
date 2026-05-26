"""
Integration tests — hit the real OANDA practice account.
Requires .env with valid credentials. Read-only calls only (no trades placed).

Run with: pytest tests/test_integration.py -v
"""

import threading
from datetime import datetime, timezone
import pytest
import config
from oanda_api import OandaAPI
from oanda_stream import OandaStream
from candle_aggregator import CandleAggregator

needs_credentials = pytest.mark.skipif(
    not config.API_TOKEN or not config.ACCOUNT_ID,
    reason="Missing OANDA credentials in .env",
)


def _forex_market_open():
    """Forex is closed Friday 21:00 UTC through Sunday 21:00 UTC."""
    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # Mon=0 ... Sun=6
    if weekday == 5:  # Saturday
        return False
    if weekday == 4 and now.hour >= 21:  # Friday after 21:00 UTC
        return False
    if weekday == 6 and now.hour < 21:  # Sunday before 21:00 UTC
        return False
    return True


needs_market_open = pytest.mark.skipif(
    not _forex_market_open(),
    reason="Forex market closed (weekend) — streaming tests need live ticks",
)

@pytest.fixture
def api():
    return OandaAPI(config.API_TOKEN, config.ACCOUNT_ID, config.BASE_URL)


@needs_credentials
class TestAccountConnection:
    def test_account_summary_returns_balance(self, api):
        data = api.get_account_summary()
        acct = data["account"]
        assert "balance" in acct
        assert float(acct["balance"]) > 0

    def test_account_summary_has_currency(self, api):
        data = api.get_account_summary()
        # Practice accounts can be in various base currencies — don't hardcode USD
        assert "currency" in data["account"]


@needs_credentials
class TestPricing:
    def test_get_price_returns_bid_ask(self, api):
        data = api.get_price("EUR_USD")
        price = data["prices"][0]
        assert "bids" in price
        assert "asks" in price
        bid = float(price["bids"][0]["price"])
        ask = float(price["asks"][0]["price"])
        assert bid > 0
        assert ask > 0
        assert ask >= bid


@needs_credentials
class TestCandles:
    def test_get_candles_returns_data(self, api):
        data = api.get_candles("EUR_USD", "H1", 5)
        assert "candles" in data
        # OANDA returns fewer when the window includes market-closed periods,
        # so accept any non-empty result up to the requested count
        assert 1 <= len(data["candles"]) <= 5

    def test_candle_has_ohlc(self, api):
        data = api.get_candles("EUR_USD", "H1", 1)
        candle = data["candles"][0]
        mid = candle["mid"]
        for field in ("o", "h", "l", "c"):
            assert field in mid
            assert float(mid[field]) > 0

    def test_candle_has_volume_and_time(self, api):
        data = api.get_candles("EUR_USD", "H1", 1)
        candle = data["candles"][0]
        assert "time" in candle
        assert "volume" in candle


@needs_credentials
class TestPositions:
    def test_get_open_positions_returns_list(self, api):
        data = api.get_open_positions()
        assert "positions" in data
        assert isinstance(data["positions"], list)


@needs_credentials
@needs_market_open
class TestStreaming:
    def test_stream_receives_price_ticks(self):
        """Connect to the live stream and verify at least one PRICE event arrives.

        Markets must be open for this to pass. Uses a 60s safety timeout so
        the test never hangs indefinitely.
        """
        stream = OandaStream(config.API_TOKEN, config.ACCOUNT_ID, config.BASE_URL)
        received = []

        def on_price(data):
            received.append(data)
            if len(received) >= 2:
                stream.stop()

        stream.on_price(on_price)

        timer = threading.Timer(60.0, stream.stop)
        timer.start()
        try:
            stream.start(["EUR_USD"], max_reconnects=2)
        finally:
            timer.cancel()

        assert len(received) >= 1, "No price ticks received (markets closed?)"
        tick = received[0]
        assert tick["type"] == "PRICE"
        assert tick["instrument"] == "EUR_USD"
        assert "bids" in tick
        assert "asks" in tick


@needs_credentials
@needs_market_open
class TestCandleAggregation:
    def test_live_stream_produces_m1_candle_close(self):
        """Stream EUR/USD ticks and wait for an M1 candle to close.

        Markets must be open. Waits up to 90s for at least one candle close.
        """
        stream = OandaStream(config.API_TOKEN, config.ACCOUNT_ID, config.BASE_URL)
        aggregator = CandleAggregator(["M1"])
        closed_candles = []

        def on_close(granularity, candle):
            closed_candles.append((granularity, candle))
            stream.stop()

        aggregator.on_candle_close(on_close)
        stream.on_price(aggregator.on_tick)

        timer = threading.Timer(90.0, stream.stop)
        timer.start()
        try:
            stream.start(["EUR_USD"], max_reconnects=2)
        finally:
            timer.cancel()

        assert len(closed_candles) >= 1, "No M1 candle closed within 90s (markets closed?)"
        granularity, candle = closed_candles[0]
        assert granularity == "M1"
        assert candle["instrument"] == "EUR_USD"
        # Volume should be a positive count of ticks received in the bucket
        assert candle["volume"] > 0
        # OHLC values should all be within reasonable EUR/USD range
        for field in ("open", "high", "low", "close"):
            assert 0.5 < candle[field] < 2.0
        # H >= O,C >= L invariant
        assert candle["high"] >= candle["open"]
        assert candle["high"] >= candle["close"]
        assert candle["low"] <= candle["open"]
        assert candle["low"] <= candle["close"]


@needs_credentials
class TestTransactions:
    def test_get_transactions_returns_list(self, api):
        data = api.get_transactions()
        assert "transactions" in data
        assert isinstance(data["transactions"], list)

    def test_transactions_have_required_fields(self, api):
        data = api.get_transactions()
        if data["transactions"]:
            tx = data["transactions"][0]
            assert "id" in tx
            assert "type" in tx
            assert "time" in tx
