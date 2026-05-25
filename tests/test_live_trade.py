"""
LIVE TRADE TEST — places a real order on the OANDA PRACTICE account.

This test is SKIPPED by default. To run it, set the env var:
    RUN_LIVE_TRADE_TESTS=1 pytest tests/test_live_trade.py -v

Safety:
- Only runs against the practice environment (refuses to run on live)
- Uses 1 unit of EUR/USD (smallest possible order, ~$1 exposure)
- Closes the position immediately after opening
- Never places more than one open position at a time
"""

import os
import time
import pytest
import config
from oanda_api import OandaAPI

INSTRUMENT = "EUR_USD"
UNITS = 1

skip_unless_enabled = pytest.mark.skipif(
    os.getenv("RUN_LIVE_TRADE_TESTS") != "1",
    reason="Set RUN_LIVE_TRADE_TESTS=1 to enable live trade tests",
)

skip_if_live_env = pytest.mark.skipif(
    "fxpractice" not in (config.BASE_URL or ""),
    reason="Live trade tests only allowed on practice environment",
)


@pytest.fixture
def api():
    return OandaAPI(config.API_TOKEN, config.ACCOUNT_ID, config.BASE_URL)


@skip_unless_enabled
@skip_if_live_env
class TestLiveTrade:
    def test_place_and_close_market_order(self, api):
        # Place buy order
        order_response = api.place_market_order(INSTRUMENT, UNITS)
        assert "orderFillTransaction" in order_response, (
            f"Order did not fill. Response: {order_response}"
        )

        fill = order_response["orderFillTransaction"]
        assert fill["instrument"] == INSTRUMENT
        assert int(fill["units"]) == UNITS
        assert float(fill["price"]) > 0

        # Give OANDA a moment to register the position before closing
        time.sleep(1)

        # Verify position is open
        positions = api.get_open_positions()
        eur_positions = [p for p in positions["positions"] if p["instrument"] == INSTRUMENT]
        assert len(eur_positions) == 1, "Expected exactly one EUR_USD position open"

        # Close the position
        close_response = api.close_position(INSTRUMENT)
        assert "longOrderFillTransaction" in close_response, (
            f"Close did not fill. Response: {close_response}"
        )

        close_fill = close_response["longOrderFillTransaction"]
        assert close_fill["instrument"] == INSTRUMENT
        assert int(close_fill["units"]) == -UNITS
        assert "pl" in close_fill

    def test_no_open_positions_after_test(self, api):
        """Sanity check that the previous test cleaned up after itself."""
        positions = api.get_open_positions()
        eur_positions = [p for p in positions["positions"] if p["instrument"] == INSTRUMENT]
        # If a position remains, the previous test failed to clean up — fail loudly
        assert len(eur_positions) == 0, (
            "EUR_USD position still open from previous test — close it manually"
        )
