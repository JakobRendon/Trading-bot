import json
from unittest.mock import patch, MagicMock
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
    # Make it work as a context manager
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

class TestEventDispatch:
    @patch("oanda_stream.requests.get")
    def test_price_event_triggers_price_callback(self, mock_get):
        price_event = json.dumps({"type": "PRICE", "instrument": "EUR_USD"}).encode()
        mock_get.return_value = mock_stream_response([price_event])

        stream = make_stream()
        received = []
        stream.on_price(received.append)
        stream.start(["EUR_USD"])

        assert len(received) == 1
        assert received[0]["instrument"] == "EUR_USD"

    @patch("oanda_stream.requests.get")
    def test_heartbeat_event_triggers_heartbeat_callback(self, mock_get):
        heartbeat = json.dumps({"type": "HEARTBEAT", "time": "2026-05-25T00:00:00Z"}).encode()
        mock_get.return_value = mock_stream_response([heartbeat])

        stream = make_stream()
        received = []
        stream.on_heartbeat(received.append)
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
        stream.on_price(prices.append)
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
        stream.on_price(received.append)
        stream.start(["EUR_USD"])

        assert len(received) == 1

    @patch("oanda_stream.requests.get")
    def test_unknown_event_types_are_ignored(self, mock_get):
        event = json.dumps({"type": "UNKNOWN_TYPE"}).encode()
        mock_get.return_value = mock_stream_response([event])

        stream = make_stream()
        received = []
        stream.on_price(received.append)
        stream.on_heartbeat(received.append)
        stream.start(["EUR_USD"])

        assert len(received) == 0


# --- Request format ---

class TestRequestFormat:
    @patch("oanda_stream.requests.get")
    def test_url_includes_account_and_pricing_stream(self, mock_get):
        mock_get.return_value = mock_stream_response([])

        stream = make_stream()
        stream.start(["EUR_USD"])

        called_url = mock_get.call_args[0][0]
        assert f"/v3/accounts/{ACCOUNT}/pricing/stream" in called_url
        assert "stream-fxpractice.oanda.com" in called_url

    @patch("oanda_stream.requests.get")
    def test_instruments_passed_as_comma_separated(self, mock_get):
        mock_get.return_value = mock_stream_response([])

        stream = make_stream()
        stream.start(["EUR_USD", "GBP_USD", "USD_JPY"])

        params = mock_get.call_args[1]["params"]
        assert params["instruments"] == "EUR_USD,GBP_USD,USD_JPY"

    @patch("oanda_stream.requests.get")
    def test_uses_stream_true_for_chunked_transfer(self, mock_get):
        mock_get.return_value = mock_stream_response([])

        stream = make_stream()
        stream.start(["EUR_USD"])

        assert mock_get.call_args[1]["stream"] is True


# --- Stop behavior ---

class TestStop:
    @patch("oanda_stream.requests.get")
    def test_stop_exits_loop(self, mock_get):
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


# --- Error handling ---

class TestErrorHandling:
    @patch("oanda_stream.requests.get")
    def test_non_ok_response_exits_without_callback(self, mock_get, capsys):
        mock_get.return_value = mock_stream_response([], ok=False, status_code=401)

        stream = make_stream()
        received = []
        stream.on_price(received.append)
        stream.start(["EUR_USD"])

        assert len(received) == 0
        captured = capsys.readouterr()
        assert "Stream error 401" in captured.out
