"""
Integration tests — hit the real OANDA practice account.
Requires .env with valid credentials. Read-only calls only (no trades placed).

Run with: pytest tests/test_integration.py -v
"""

import threading
import pytest
import config
from oanda_api import OandaAPI
from oanda_stream import OandaStream

needs_credentials = pytest.mark.skipif(
    not config.API_TOKEN or not config.ACCOUNT_ID,
    reason="Missing OANDA credentials in .env",
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

    def test_account_summary_returns_currency(self, api):
        data = api.get_account_summary()
        assert data["account"]["currency"] == "USD"


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
        assert len(data["candles"]) == 5

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
            stream.start(["EUR_USD"])
        finally:
            timer.cancel()

        assert len(received) >= 1, "No price ticks received (markets closed?)"
        tick = received[0]
        assert tick["type"] == "PRICE"
        assert tick["instrument"] == "EUR_USD"
        assert "bids" in tick
        assert "asks" in tick


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
