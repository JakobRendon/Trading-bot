import json
from unittest.mock import patch, MagicMock
import pytest
from oanda_api import OandaAPI, OandaAPIError, OandaOrderRejected

BASE = "https://api-fxpractice.oanda.com"
TOKEN = "fake-token"
ACCOUNT = "101-001-0000000-001"


def make_api():
    return OandaAPI(TOKEN, ACCOUNT, BASE)


def mock_response(json_data, status_code=200, headers=None):
    resp = MagicMock()
    # `requests.Response.ok` is True for any 2xx, not just 200
    resp.ok = 200 <= status_code < 300
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = json.dumps(json_data)
    resp.headers = headers or {}
    return resp


def position_response(long_units=0, short_units=0):
    return mock_response({
        "position": {
            "instrument": "EUR_USD",
            "long": {"units": str(long_units)},
            "short": {"units": str(short_units)},
        }
    })


def patch_request(*responses):
    """Patch Session.request to return the given response(s) in sequence.

    If a single response is provided, every call returns it. If multiple,
    returns them in order (one per call).
    """
    if len(responses) == 1:
        return patch("oanda_api.requests.Session.request", return_value=responses[0])
    return patch("oanda_api.requests.Session.request", side_effect=list(responses))


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
        with patch_request(mock_response({"candles": []})) as mock_req:
            api.get_candles("EUR_USD")
            method, url = mock_req.call_args[0]
            assert method == "GET"
            assert f"/accounts/{ACCOUNT}" not in url
            assert "/v3/instruments/EUR_USD/candles" in url

    def test_pricing_uses_account_url(self):
        api = make_api()
        with patch_request(mock_response({"prices": []})) as mock_req:
            api.get_price("EUR_USD")
            url = mock_req.call_args[0][1]
            assert f"/accounts/{ACCOUNT}/pricing" in url


# --- Headers ---

class TestHeaders:
    def test_auth_header(self):
        api = make_api()
        assert api.session.headers["Authorization"] == f"Bearer {TOKEN}"

    def test_content_type(self):
        api = make_api()
        assert api.session.headers["Content-Type"] == "application/json"

    def test_user_agent_set(self):
        """OANDA recommends a descriptive User-Agent for support traceability."""
        api = make_api()
        ua = api.session.headers["User-Agent"]
        assert "OandaBot" in ua


# --- Session / connection reuse ---

class TestSession:
    def test_session_is_created(self):
        """OandaAPI should hold a persistent requests.Session for connection reuse."""
        import requests as req_module
        api = make_api()
        assert isinstance(api.session, req_module.Session)

    def test_close_releases_session(self):
        api = make_api()
        api.close()
        # No assertion needed — just verify close() exists and doesn't raise

    def test_context_manager_closes_session(self):
        with make_api() as api:
            assert api.session is not None


# --- Request parameters ---

class TestRequestParams:
    def test_candles_default_params(self):
        api = make_api()
        with patch_request(mock_response({"candles": []})) as mock_req:
            api.get_candles("GBP_USD")
            params = mock_req.call_args[1]["params"]
            assert params["granularity"] == "M1"
            assert params["count"] == 10

    def test_candles_custom_params(self):
        api = make_api()
        with patch_request(mock_response({"candles": []})) as mock_req:
            api.get_candles("GBP_USD", granularity="H1", count=50)
            params = mock_req.call_args[1]["params"]
            assert params["granularity"] == "H1"
            assert params["count"] == 50

    def test_price_instrument_param(self):
        api = make_api()
        with patch_request(mock_response({"prices": []})) as mock_req:
            api.get_price("USD_JPY")
            params = mock_req.call_args[1]["params"]
            assert params["instruments"] == "USD_JPY"


# --- Transactions ---

class TestTransactions:
    def test_default_since_id_is_0(self):
        """since_id='0' returns ALL transactions including ID 1 (account creation).
        Previously defaulted to '1' which silently skipped the first transaction."""
        api = make_api()
        with patch_request(mock_response({"transactions": []})) as mock_req:
            api.get_transactions()
            params = mock_req.call_args[1]["params"]
            assert params["id"] == "0"

    def test_custom_since_id_converted_to_string(self):
        api = make_api()
        with patch_request(mock_response({"transactions": []})) as mock_req:
            api.get_transactions(since_id=42)
            params = mock_req.call_args[1]["params"]
            assert params["id"] == "42"

    def test_transactions_url(self):
        api = make_api()
        with patch_request(mock_response({"transactions": []})) as mock_req:
            api.get_transactions()
            url = mock_req.call_args[0][1]
            assert f"/accounts/{ACCOUNT}/transactions/sinceid" in url


# --- Order payload ---

class TestOrderPayload:
    def test_market_order_buy(self):
        api = make_api()
        with patch_request(mock_response({"orderFillTransaction": {}})) as mock_req:
            api.place_market_order("EUR_USD", 100)
            method, url = mock_req.call_args[0]
            payload = mock_req.call_args[1]["json"]
            assert method == "POST"
            assert payload["order"]["type"] == "MARKET"
            assert payload["order"]["instrument"] == "EUR_USD"
            assert payload["order"]["units"] == "100"
            assert payload["order"]["timeInForce"] == "FOK"
            assert payload["order"]["positionFill"] == "DEFAULT"

    def test_market_order_sell_negative_units(self):
        api = make_api()
        with patch_request(mock_response({"orderFillTransaction": {}})) as mock_req:
            api.place_market_order("EUR_USD", -500)
            payload = mock_req.call_args[1]["json"]
            assert payload["order"]["units"] == "-500"

    def test_units_passed_as_string(self):
        """OANDA requires units as a string, not an integer."""
        api = make_api()
        with patch_request(mock_response({"orderFillTransaction": {}})) as mock_req:
            api.place_market_order("EUR_USD", 100)
            payload = mock_req.call_args[1]["json"]
            assert isinstance(payload["order"]["units"], str)


# --- Order rejection in 2xx body ---

class TestOrderRejectionInBody:
    """OANDA can return HTTP 2xx with an orderRejectTransaction in the body
    (validation failure). place_market_order should raise OandaOrderRejected."""

    def test_2xx_with_orderRejectTransaction_raises(self):
        api = make_api()
        reject_body = {
            "orderRejectTransaction": {
                "type": "MARKET_ORDER_REJECT",
                "rejectReason": "INSUFFICIENT_MARGIN",
            }
        }
        with patch_request(mock_response(reject_body, 201)):
            with pytest.raises(OandaOrderRejected) as exc_info:
                api.place_market_order("EUR_USD", 100)
            assert "INSUFFICIENT_MARGIN" in str(exc_info.value)

    def test_orderFillTransaction_returns_normally(self):
        api = make_api()
        fill_body = {"orderFillTransaction": {"price": "1.10000"}}
        with patch_request(mock_response(fill_body, 201)):
            result = api.place_market_order("EUR_USD", 100)
            assert "orderFillTransaction" in result

    def test_orderCancelTransaction_returns_normally(self):
        """FOK order couldn't fill — returns body with cancel, not raise."""
        api = make_api()
        cancel_body = {
            "orderCreateTransaction": {},
            "orderCancelTransaction": {"reason": "MARKET_HALTED"},
        }
        with patch_request(mock_response(cancel_body, 201)):
            result = api.place_market_order("EUR_USD", 100)
            assert "orderCancelTransaction" in result


# --- Close position payload ---

class TestClosePosition:
    def test_close_long_only_position_sends_only_long(self):
        api = make_api()
        with patch_request(position_response(long_units=100), mock_response({})) as mock_req:
            api.close_position("EUR_USD")
            put_call = mock_req.call_args_list[1]
            assert put_call.args[0] == "PUT"
            assert put_call.kwargs["json"] == {"longUnits": "ALL"}

    def test_close_short_only_position_sends_only_short(self):
        api = make_api()
        with patch_request(position_response(short_units=-100), mock_response({})) as mock_req:
            api.close_position("EUR_USD")
            put_call = mock_req.call_args_list[1]
            assert put_call.kwargs["json"] == {"shortUnits": "ALL"}

    def test_close_both_sides_when_both_exist(self):
        api = make_api()
        with patch_request(position_response(long_units=100, short_units=-50), mock_response({})) as mock_req:
            api.close_position("EUR_USD")
            put_call = mock_req.call_args_list[1]
            assert put_call.kwargs["json"] == {"longUnits": "ALL", "shortUnits": "ALL"}

    def test_close_no_position_skips_put_call(self):
        api = make_api()
        with patch_request(position_response()) as mock_req:
            result = api.close_position("EUR_USD")
            # Only the GET happened
            assert mock_req.call_count == 1
            assert mock_req.call_args.args[0] == "GET"
            assert result.get("noPosition") is True
            assert result.get("instrument") == "EUR_USD"

    def test_close_propagates_position_query_failure(self):
        """If the position query fails (e.g. auth), close_position should raise,
        not silently return 'no position'."""
        api = make_api()
        with patch_request(mock_response({"errorMessage": "Unauthorized"}, 401)):
            with pytest.raises(OandaAPIError) as exc_info:
                api.close_position("EUR_USD")
            assert exc_info.value.status_code == 401

    def test_close_url_includes_instrument(self):
        api = make_api()
        with patch_request(position_response(long_units=100), mock_response({})) as mock_req:
            api.close_position("GBP_USD")
            put_call = mock_req.call_args_list[1]
            assert "/positions/GBP_USD/close" in put_call.args[1]


# --- Timeouts ---

class TestTimeouts:
    def test_get_uses_connect_read_tuple(self):
        """OANDA best practices suggest separate connect and read timeouts."""
        api = make_api()
        with patch_request(mock_response({})) as mock_req:
            api.get_account_summary()
            timeout = mock_req.call_args[1]["timeout"]
            assert isinstance(timeout, tuple)
            assert len(timeout) == 2

    def test_post_has_longer_read_timeout_than_get(self):
        """POST /orders gets a longer read timeout because timing out mid-fill
        leaves the order in an ambiguous state."""
        api = make_api()
        with patch_request(mock_response({"orderFillTransaction": {}})) as mock_req:
            api.place_market_order("EUR_USD", 100)
            post_timeout = mock_req.call_args[1]["timeout"]
        with patch_request(mock_response({})) as mock_req2:
            api.get_account_summary()
            get_timeout = mock_req2.call_args[1]["timeout"]
        assert post_timeout[1] > get_timeout[1]


# --- Rate limit (429) handling ---

class TestRateLimitRetry:
    @patch("oanda_api.time.sleep")
    def test_429_with_retry_after_retries_once(self, mock_sleep):
        """429 should trigger one retry, respecting Retry-After header."""
        api = make_api()
        rate_limited = mock_response({"err": "rate limited"}, 429, headers={"Retry-After": "2"})
        success = mock_response({"account": {}})
        with patch_request(rate_limited, success):
            api.get_account_summary()
        mock_sleep.assert_called_once_with(2.0)

    @patch("oanda_api.time.sleep")
    def test_429_without_retry_after_uses_default(self, mock_sleep):
        api = make_api()
        rate_limited = mock_response({"err": "rate limited"}, 429)
        success = mock_response({"account": {}})
        with patch_request(rate_limited, success):
            api.get_account_summary()
        # No Retry-After → default 1s
        mock_sleep.assert_called_once_with(1.0)

    @patch("oanda_api.time.sleep")
    def test_429_caps_wait_at_30s(self, mock_sleep):
        api = make_api()
        rate_limited = mock_response({"err": "rate limited"}, 429, headers={"Retry-After": "120"})
        success = mock_response({"account": {}})
        with patch_request(rate_limited, success):
            api.get_account_summary()
        # 120s capped at 30s
        mock_sleep.assert_called_once_with(30)

    @patch("oanda_api.time.sleep")
    def test_429_only_retries_once(self, mock_sleep):
        """If still 429 after retry, raise — don't loop forever."""
        api = make_api()
        rate_limited = mock_response({"err": "rate limited"}, 429, headers={"Retry-After": "1"})
        with patch_request(rate_limited, rate_limited):
            with pytest.raises(OandaAPIError) as exc_info:
                api.get_account_summary()
            assert exc_info.value.status_code == 429


# --- Error handling ---

class TestErrorHandling:
    def test_get_raises_on_401(self):
        api = make_api()
        with patch_request(mock_response({"errorMessage": "Invalid"}, 401)):
            with pytest.raises(OandaAPIError) as exc_info:
                api.get_account_summary()
            assert exc_info.value.status_code == 401

    def test_get_raises_on_5xx(self):
        api = make_api()
        with patch_request(mock_response({"err": "boom"}, 503)):
            with pytest.raises(OandaAPIError) as exc_info:
                api.get_account_summary()
            assert exc_info.value.status_code == 503

    def test_post_raises_on_400(self):
        api = make_api()
        with patch_request(mock_response({"orderRejectTransaction": {}}, 400)):
            with pytest.raises(OandaAPIError):
                api.place_market_order("EUR_USD", 100)

    def test_2xx_accepted_not_just_200(self):
        """OANDA returns 201 for POST /orders on success."""
        api = make_api()
        with patch_request(mock_response({"orderFillTransaction": {}}, 201)):
            result = api.place_market_order("EUR_USD", 100)
            assert result == {"orderFillTransaction": {}}
