import json
import threading
import time
from unittest.mock import patch, MagicMock
import requests
from oanda_stream import OandaStream

BASE = "https://api-fxpractice.oanda.com"
TOKEN = "fake-token"
ACCOUNT = "101-001-0000000-001"


def make_stream():
    return OandaStream(TOKEN, ACCOUNT, BASE)


def mock_stream_response(lines, ok=True, status_code=200):
    """Build a context-managed response mock that yields the given lines."""
    resp = MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.text = "" if ok else "error"
    resp.iter_lines.return_value = iter(lines)
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# --- URL & header construction ---

class TestStreamConfiguration:
    def test_stream_url_uses_stream_subdomain(self):
        stream = make_stream()
        assert stream.stream_url == "https://stream-fxpractice.oanda.com"

    def test_stream_url_live_environment(self):
        stream = OandaStream(TOKEN, ACCOUNT, "https://api-fxtrade.oanda.com")
        assert stream.stream_url == "https://stream-fxtrade.oanda.com"

    def test_auth_header(self):
        stream = make_stream()
        assert stream.headers["Authorization"] == f"Bearer {TOKEN}"


# --- Callback registration ---

class TestCallbackRegistration:
    def test_on_price_registers_callback(self):
        stream = make_stream()
        cb = lambda data: None
        stream.on_price(cb)
        assert cb in stream.price_callbacks

    def test_on_heartbeat_registers_callback(self):
        stream = make_stream()
        cb = lambda data: None
        stream.on_heartbeat(cb)
        assert cb in stream.heartbeat_callbacks

    def test_multiple_callbacks_supported(self):
        stream = make_stream()
        stream.on_price(lambda d: None)
        stream.on_price(lambda d: None)
        assert len(stream.price_callbacks) == 2


# --- Event dispatch ---
#
# The stream now reconnects on clean close, so unit tests use a callback that
# calls stop() after observing the expected events. Otherwise the loop would
# keep reconnecting against the same mock forever.

def stop_after_n(stream, n):
    """Return a callback that calls stream.stop() after n events."""
    counter = {"n": 0}

    def cb(data):
        counter["n"] += 1
        if counter["n"] >= n:
            stream.stop()
    return cb


class TestEventDispatch:
    @patch("oanda_stream.requests.get")
    def test_price_event_triggers_price_callback(self, mock_get):
        price_event = json.dumps({"type": "PRICE", "instrument": "EUR_USD"}).encode()
        mock_get.return_value = mock_stream_response([price_event])

        stream = make_stream()
        received = []

        def on_price(data):
            received.append(data)
            stream.stop()

        stream.on_price(on_price)
        stream.start(["EUR_USD"])

        assert len(received) == 1
        assert received[0]["instrument"] == "EUR_USD"

    @patch("oanda_stream.requests.get")
    def test_heartbeat_event_triggers_heartbeat_callback(self, mock_get):
        heartbeat = json.dumps({"type": "HEARTBEAT", "time": "2026-05-25T00:00:00Z"}).encode()
        mock_get.return_value = mock_stream_response([heartbeat])

        stream = make_stream()
        received = []

        def on_heartbeat(data):
            received.append(data)
            stream.stop()

        stream.on_heartbeat(on_heartbeat)
        stream.start(["EUR_USD"])

        assert len(received) == 1
        assert received[0]["type"] == "HEARTBEAT"

    @patch("oanda_stream.requests.get")
    def test_price_and_heartbeat_dispatch_to_different_callbacks(self, mock_get):
        events = [
            json.dumps({"type": "PRICE", "instrument": "EUR_USD"}).encode(),
            json.dumps({"type": "HEARTBEAT"}).encode(),
            json.dumps({"type": "PRICE", "instrument": "EUR_USD"}).encode(),
        ]
        mock_get.return_value = mock_stream_response(events)

        stream = make_stream()
        prices = []
        heartbeats = []

        def on_price(data):
            prices.append(data)
            if len(prices) >= 2:
                stream.stop()

        stream.on_price(on_price)
        stream.on_heartbeat(heartbeats.append)
        stream.start(["EUR_USD"])

        assert len(prices) == 2
        assert len(heartbeats) == 1

    @patch("oanda_stream.requests.get")
    def test_invalid_json_lines_are_skipped(self, mock_get):
        events = [
            b"not json",
            json.dumps({"type": "PRICE", "instrument": "EUR_USD"}).encode(),
            b"",
        ]
        mock_get.return_value = mock_stream_response(events)

        stream = make_stream()
        received = []

        def on_price(data):
            received.append(data)
            stream.stop()

        stream.on_price(on_price)
        stream.start(["EUR_USD"])

        assert len(received) == 1

    @patch("oanda_stream.requests.get")
    def test_unknown_event_types_are_ignored(self, mock_get):
        # No callback ever fires, so cap reconnects to exit quickly.
        event = json.dumps({"type": "UNKNOWN_TYPE"}).encode()
        mock_get.return_value = mock_stream_response([event])

        stream = make_stream()
        received = []
        stream.on_price(received.append)
        stream.on_heartbeat(received.append)

        # Patch sleep so the reconnect backoff doesn't block the test.
        with patch("oanda_stream.time.sleep"):
            stream.start(["EUR_USD"], max_reconnects=0)

        assert len(received) == 0


# --- Request format ---

class TestRequestFormat:
    @patch("oanda_stream.requests.get")
    def test_url_includes_account_and_pricing_stream(self, mock_get):
        mock_get.return_value = mock_stream_response([])

        stream = make_stream()
        with patch("oanda_stream.time.sleep"):
            stream.start(["EUR_USD"], max_reconnects=0)

        called_url = mock_get.call_args[0][0]
        assert f"/v3/accounts/{ACCOUNT}/pricing/stream" in called_url
        assert "stream-fxpractice.oanda.com" in called_url

    @patch("oanda_stream.requests.get")
    def test_instruments_passed_as_comma_separated(self, mock_get):
        mock_get.return_value = mock_stream_response([])

        stream = make_stream()
        with patch("oanda_stream.time.sleep"):
            stream.start(["EUR_USD", "GBP_USD", "USD_JPY"], max_reconnects=0)

        params = mock_get.call_args[1]["params"]
        assert params["instruments"] == "EUR_USD,GBP_USD,USD_JPY"

    @patch("oanda_stream.requests.get")
    def test_uses_stream_true_for_chunked_transfer(self, mock_get):
        mock_get.return_value = mock_stream_response([])

        stream = make_stream()
        with patch("oanda_stream.time.sleep"):
            stream.start(["EUR_USD"], max_reconnects=0)

        assert mock_get.call_args[1]["stream"] is True

    @patch("oanda_stream.requests.get")
    def test_uses_heartbeat_timeout(self, mock_get):
        """Socket timeout should be HEARTBEAT_TIMEOUT (10s), tight enough to catch
        a stalled feed within 2x the OANDA 5s heartbeat interval."""
        mock_get.return_value = mock_stream_response([])

        stream = make_stream()
        with patch("oanda_stream.time.sleep"):
            stream.start(["EUR_USD"], max_reconnects=0)

        assert mock_get.call_args[1]["timeout"] == 10


# --- Stop behavior ---

class TestStop:
    @patch("oanda_stream.requests.get")
    def test_stop_in_callback_exits_loop(self, mock_get):
        events = [
            json.dumps({"type": "PRICE", "instrument": "EUR_USD"}).encode(),
            json.dumps({"type": "PRICE", "instrument": "EUR_USD"}).encode(),
            json.dumps({"type": "PRICE", "instrument": "EUR_USD"}).encode(),
        ]
        mock_get.return_value = mock_stream_response(events)

        stream = make_stream()
        received = []

        def stop_after_first(data):
            received.append(data)
            stream.stop()

        stream.on_price(stop_after_first)
        stream.start(["EUR_USD"])

        assert len(received) == 1


# --- Reconnect behavior ---

class TestReconnect:
    @patch("oanda_stream.time.sleep")  # skip backoff sleeps
    @patch("oanda_stream.requests.get")
    def test_clean_disconnect_triggers_reconnect(self, mock_get, mock_sleep):
        """When iter_lines exhausts without exception (server clean close),
        the stream should reconnect rather than silently exit."""
        # First connect yields one price + closes; subsequent connects yield empty.
        mock_get.side_effect = [
            mock_stream_response([json.dumps({"type": "PRICE", "instrument": "EUR_USD"}).encode()]),
            mock_stream_response([]),
            mock_stream_response([]),
        ]

        stream = make_stream()
        received = []
        stream.on_price(received.append)

        stream.start(["EUR_USD"], max_reconnects=2)

        # Should have attempted multiple connects (at least the first + reconnects)
        assert mock_get.call_count >= 2

    @patch("oanda_stream.time.sleep")
    @patch("oanda_stream.requests.get")
    def test_connection_error_triggers_reconnect(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            requests.ConnectionError("boom"),
            mock_stream_response([]),
        ]

        stream = make_stream()
        stream.start(["EUR_USD"], max_reconnects=1)

        assert mock_get.call_count == 2

    @patch("oanda_stream.time.sleep")
    @patch("oanda_stream.requests.get")
    def test_timeout_triggers_reconnect(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            requests.Timeout("stalled"),
            mock_stream_response([]),
        ]

        stream = make_stream()
        stream.start(["EUR_USD"], max_reconnects=1)

        assert mock_get.call_count == 2

    @patch("oanda_stream.time.sleep")
    @patch("oanda_stream.requests.get")
    def test_5xx_response_triggers_reconnect(self, mock_get, mock_sleep):
        """5xx is transient (OANDA maintenance, etc.) — should retry, not give up."""
        mock_get.side_effect = [
            mock_stream_response([], ok=False, status_code=503),
            mock_stream_response([]),
        ]

        stream = make_stream()
        stream.start(["EUR_USD"], max_reconnects=1)

        assert mock_get.call_count == 2

    @patch("oanda_stream.time.sleep")
    @patch("oanda_stream.requests.get")
    def test_4xx_response_exits_immediately(self, mock_get, mock_sleep):
        """4xx is a client/auth error — no point retrying."""
        mock_get.return_value = mock_stream_response([], ok=False, status_code=401)

        stream = make_stream()
        stream.start(["EUR_USD"], max_reconnects=5)

        assert mock_get.call_count == 1  # No retry

    @patch("oanda_stream.time.sleep")
    @patch("oanda_stream.requests.get")
    def test_max_reconnects_bounded(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            requests.ConnectionError("boom"),
            requests.ConnectionError("boom"),
            requests.ConnectionError("boom"),
            requests.ConnectionError("boom"),
            requests.ConnectionError("boom"),
        ]

        stream = make_stream()
        stream.start(["EUR_USD"], max_reconnects=2)

        # 1 initial + 2 reconnect attempts = 3 total
        assert mock_get.call_count == 3

    @patch("oanda_stream.time.sleep")
    @patch("oanda_stream.requests.get")
    def test_receiving_data_resets_reconnect_counter(self, mock_get, mock_sleep):
        """Transient disconnects shouldn't accumulate toward give-up if we
        successfully received data in between."""
        # Connect 1: get a price, then clean close
        # Connect 2: get a price, then clean close
        # Connect 3: get a price, then call stop()
        def price():
            return json.dumps({"type": "PRICE", "instrument": "EUR_USD"}).encode()

        mock_get.side_effect = [
            mock_stream_response([price()]),
            mock_stream_response([price()]),
            mock_stream_response([price()]),
        ]

        stream = make_stream()
        received = []

        def on_price(data):
            received.append(data)
            if len(received) >= 3:
                stream.stop()

        stream.on_price(on_price)
        # max_reconnects=1 — would normally allow only 2 total attempts, but
        # since each receives data, the counter resets each time.
        stream.start(["EUR_USD"], max_reconnects=1)

        assert len(received) == 3

    @patch("oanda_stream.time.sleep")
    @patch("oanda_stream.requests.get")
    def test_backoff_doubles_then_caps_at_60(self, mock_get, mock_sleep):
        mock_get.side_effect = [requests.ConnectionError("e")] * 10

        stream = make_stream()
        stream.start(["EUR_USD"], max_reconnects=8)

        # Backoff schedule: 1, 2, 4, 8, 16, 32, 60, 60, 60
        sleep_values = [call.args[0] for call in mock_sleep.call_args_list]
        assert sleep_values[:7] == [1, 2, 4, 8, 16, 32, 60]
        assert all(v == 60 for v in sleep_values[6:])

    @patch("oanda_stream.time.sleep")
    @patch("oanda_stream.requests.get")
    def test_stop_during_reconnect_wait_exits(self, mock_get, mock_sleep):
        mock_get.side_effect = requests.ConnectionError("e")

        stream = make_stream()
        # Set stop=True inside the sleep call so the next iteration sees it
        mock_sleep.side_effect = lambda _: stream.stop()

        stream.start(["EUR_USD"], max_reconnects=10)

        # First attempt fails, sleeps (and sets stop), then exits
        assert mock_get.call_count == 1
