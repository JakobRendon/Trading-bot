import json
from unittest.mock import patch, MagicMock
import pytest
from oanda_api import OandaAPI, OandaAPIError

BASE = "https://api-fxpractice.oanda.com"
TOKEN = "fake-token"
ACCOUNT = "101-001-0000000-001"


def make_api():
    return OandaAPI(TOKEN, ACCOUNT, BASE)


def mock_response(json_data, status_code=200):
    resp = MagicMock()
    # `requests.Response.ok` is True for any 2xx, not just 200
    resp.ok = 200 <= status_code < 300
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = json.dumps(json_data)
    return resp


# --- URL construction ---

class TestURLConstruction:
    def test_account_url(self):
        api = make_api()
        assert api._url("/summary") == f"{BASE}/v3/accounts/{ACCOUNT}/summary"

    def test_base_url(self):
        api = make_api()
        assert api._base_url("/instruments/EUR_USD/candles") == f"{BASE}/v3/instruments/EUR_USD/candles"

    def test_candles_uses_base_url_not_account_url(self):
        """Candles endpoint is /v3/instruments/{instrument}/candles, NOT under /accounts/."""
        api = make_api()
        with patch("oanda_api.requests.get", return_value=mock_response({"candles": []})) as mock_get:
            api.get_candles("EUR_USD")
            called_url = mock_get.call_args[0][0]
            assert f"/accounts/{ACCOUNT}" not in called_url
            assert "/v3/instruments/EUR_USD/candles" in called_url

    def test_pricing_uses_account_url(self):
        api = make_api()
        with patch("oanda_api.requests.get", return_value=mock_response({"prices": []})) as mock_get:
            api.get_price("EUR_USD")
            called_url = mock_get.call_args[0][0]
            assert f"/accounts/{ACCOUNT}/pricing" in called_url


# --- Headers ---

class TestHeaders:
    def test_auth_header(self):
        api = make_api()
        assert api.headers["Authorization"] == f"Bearer {TOKEN}"

    def test_content_type(self):
        api = make_api()
        assert api.headers["Content-Type"] == "application/json"


# --- Request parameters ---

class TestRequestParams:
    @patch("oanda_api.requests.get", return_value=mock_response({"candles": []}))
    def test_candles_default_params(self, mock_get):
        api = make_api()
        api.get_candles("GBP_USD")
        params = mock_get.call_args[1]["params"]
        assert params["granularity"] == "M1"
        assert params["count"] == 10

    @patch("oanda_api.requests.get", return_value=mock_response({"candles": []}))
    def test_candles_custom_params(self, mock_get):
        api = make_api()
        api.get_candles("GBP_USD", granularity="H1", count=50)
        params = mock_get.call_args[1]["params"]
        assert params["granularity"] == "H1"
        assert params["count"] == 50

    @patch("oanda_api.requests.get", return_value=mock_response({"prices": []}))
    def test_price_instrument_param(self, mock_get):
        api = make_api()
        api.get_price("USD_JPY")
        params = mock_get.call_args[1]["params"]
        assert params["instruments"] == "USD_JPY"


# --- Transactions ---

class TestTransactions:
    @patch("oanda_api.requests.get", return_value=mock_response({"transactions": []}))
    def test_default_since_id_is_0(self, mock_get):
        """since_id='0' returns ALL transactions including ID 1 (account creation).
        Previously defaulted to '1' which silently skipped the first transaction."""
        api = make_api()
        api.get_transactions()
        params = mock_get.call_args[1]["params"]
        assert params["id"] == "0"

    @patch("oanda_api.requests.get", return_value=mock_response({"transactions": []}))
    def test_custom_since_id_converted_to_string(self, mock_get):
        api = make_api()
        api.get_transactions(since_id=42)
        params = mock_get.call_args[1]["params"]
        assert params["id"] == "42"

    @patch("oanda_api.requests.get", return_value=mock_response({"transactions": []}))
    def test_transactions_url(self, mock_get):
        api = make_api()
        api.get_transactions()
        called_url = mock_get.call_args[0][0]
        assert f"/accounts/{ACCOUNT}/transactions/sinceid" in called_url


# --- Order payload ---

class TestOrderPayload:
    @patch("oanda_api.requests.post", return_value=mock_response({"orderFillTransaction": {}}))
    def test_market_order_buy(self, mock_post):
        api = make_api()
        api.place_market_order("EUR_USD", 100)
        payload = mock_post.call_args[1]["json"]
        assert payload["order"]["type"] == "MARKET"
        assert payload["order"]["instrument"] == "EUR_USD"
        assert payload["order"]["units"] == "100"
        assert payload["order"]["timeInForce"] == "FOK"
        assert payload["order"]["positionFill"] == "DEFAULT"

    @patch("oanda_api.requests.post", return_value=mock_response({"orderFillTransaction": {}}))
    def test_market_order_sell_negative_units(self, mock_post):
        api = make_api()
        api.place_market_order("EUR_USD", -500)
        payload = mock_post.call_args[1]["json"]
        assert payload["order"]["units"] == "-500"

    @patch("oanda_api.requests.post", return_value=mock_response({"orderFillTransaction": {}}))
    def test_units_passed_as_string(self, mock_post):
        """OANDA requires units as a string, not an integer."""
        api = make_api()
        api.place_market_order("EUR_USD", 100)
        payload = mock_post.call_args[1]["json"]
        assert isinstance(payload["order"]["units"], str)


# --- Close position payload ---

def position_response(long_units=0, short_units=0):
    return mock_response({
        "position": {
            "instrument": "EUR_USD",
            "long": {"units": str(long_units)},
            "short": {"units": str(short_units)},
        }
    })


class TestClosePosition:
    @patch("oanda_api.requests.put", return_value=mock_response({}))
    @patch("oanda_api.requests.get", return_value=position_response(long_units=100))
    def test_close_long_only_position_sends_only_long(self, mock_get, mock_put):
        api = make_api()
        api.close_position("EUR_USD")
        payload = mock_put.call_args[1]["json"]
        assert payload == {"longUnits": "ALL"}

    @patch("oanda_api.requests.put", return_value=mock_response({}))
    @patch("oanda_api.requests.get", return_value=position_response(short_units=-100))
    def test_close_short_only_position_sends_only_short(self, mock_get, mock_put):
        api = make_api()
        api.close_position("EUR_USD")
        payload = mock_put.call_args[1]["json"]
        assert payload == {"shortUnits": "ALL"}

    @patch("oanda_api.requests.put", return_value=mock_response({}))
    @patch("oanda_api.requests.get", return_value=position_response(long_units=100, short_units=-50))
    def test_close_both_sides_when_both_exist(self, mock_get, mock_put):
        api = make_api()
        api.close_position("EUR_USD")
        payload = mock_put.call_args[1]["json"]
        assert payload == {"longUnits": "ALL", "shortUnits": "ALL"}

    @patch("oanda_api.requests.put")
    @patch("oanda_api.requests.get", return_value=position_response())
    def test_close_no_position_skips_put_call(self, mock_get, mock_put):
        api = make_api()
        result = api.close_position("EUR_USD")
        mock_get.assert_called_once()  # position query must have happened
        mock_put.assert_not_called()
        assert result.get("noPosition") is True
        assert result.get("instrument") == "EUR_USD"

    @patch("oanda_api.requests.get", return_value=mock_response({"errorMessage": "Unauthorized"}, 401))
    def test_close_propagates_position_query_failure(self, mock_get):
        """If the position query fails (e.g. auth), close_position should raise,
        not silently return 'no position'."""
        api = make_api()
        with pytest.raises(OandaAPIError) as exc_info:
            api.close_position("EUR_USD")
        assert exc_info.value.status_code == 401

    @patch("oanda_api.requests.put", return_value=mock_response({}))
    @patch("oanda_api.requests.get", return_value=position_response(long_units=100))
    def test_close_url_includes_instrument(self, mock_get, mock_put):
        api = make_api()
        api.close_position("GBP_USD")
        called_url = mock_put.call_args[0][0]
        assert "/positions/GBP_USD/close" in called_url


# --- Timeouts ---

class TestTimeouts:
    @patch("oanda_api.requests.get", return_value=mock_response({}))
    def test_get_has_timeout(self, mock_get):
        api = make_api()
        api.get_account_summary()
        assert mock_get.call_args[1]["timeout"] == 10

    @patch("oanda_api.requests.post", return_value=mock_response({}))
    def test_post_has_timeout(self, mock_post):
        api = make_api()
        api.place_market_order("EUR_USD", 100)
        assert mock_post.call_args[1]["timeout"] == 10

    @patch("oanda_api.requests.put", return_value=mock_response({}))
    @patch("oanda_api.requests.get", return_value=position_response(long_units=100))
    def test_put_has_timeout(self, mock_get, mock_put):
        api = make_api()
        api.close_position("EUR_USD")
        assert mock_put.call_args[1]["timeout"] == 10


# --- Error handling ---

class TestErrorHandling:
    @patch("oanda_api.requests.get", return_value=mock_response({"errorMessage": "Invalid"}, 401))
    def test_get_raises_on_401(self, mock_get):
        api = make_api()
        with pytest.raises(OandaAPIError) as exc_info:
            api.get_account_summary()
        assert exc_info.value.status_code == 401

    @patch("oanda_api.requests.get", return_value=mock_response({"errorMessage": "Rate limited"}, 429))
    def test_get_raises_on_429(self, mock_get):
        api = make_api()
        with pytest.raises(OandaAPIError) as exc_info:
            api.get_account_summary()
        assert exc_info.value.status_code == 429

    @patch("oanda_api.requests.get", return_value=mock_response({"err": "boom"}, 503))
    def test_get_raises_on_5xx(self, mock_get):
        api = make_api()
        with pytest.raises(OandaAPIError) as exc_info:
            api.get_account_summary()
        assert exc_info.value.status_code == 503

    @patch("oanda_api.requests.post", return_value=mock_response({"orderRejectTransaction": {}}, 400))
    def test_post_raises_on_400(self, mock_post):
        api = make_api()
        with pytest.raises(OandaAPIError):
            api.place_market_order("EUR_USD", 100)

    @patch("oanda_api.requests.post", return_value=mock_response({"orderFillTransaction": {}}, 201))
    def test_2xx_accepted_not_just_200(self, mock_post):
        """OANDA returns 201 for POST /orders on success — mock_response must accept any 2xx."""
        api = make_api()
        result = api.place_market_order("EUR_USD", 100)
        assert result == {"orderFillTransaction": {}}
