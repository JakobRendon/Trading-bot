import threading
import time
import requests
from risk import pip_distance, generate_client_id, validate_risk_reward


# Per OANDA best practices, separate connect/read timeouts. The read timeout
# on order POSTs is generous because timing out mid-fill leaves the order in
# an ambiguous state — we'd rather wait than retry an order that may have filled.
TIMEOUT_DEFAULT = (3, 10)
TIMEOUT_ORDER_POST = (3, 30)

USER_AGENT = "FTMO-OandaBot/0.1 (+https://github.com/JakobRendon/Trading-bot)"


class OandaAPIError(Exception):
    """Raised when the OANDA REST API returns a non-2xx response."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"OANDA API {status_code}: {body}")


class OandaOrderRejected(Exception):
    """Raised when OANDA returns 2xx with an orderRejectTransaction body.

    A successful HTTP response can still mean the order was rejected at the
    business-logic layer (e.g. STOP_LOSS_ON_FILL_PRICE_PRECISION_EXCEEDED).
    Per https://developer.oanda.com/rest-live-v20/order-ep/
    """

    def __init__(self, reject_transaction):
        self.reject_transaction = reject_transaction
        reason = reject_transaction.get("rejectReason", "UNKNOWN")
        super().__init__(f"Order rejected: {reason}")


class OandaAPI:
    def __init__(self, api_token, account_id, base_url):
        self.account_id = account_id
        self.base_url = base_url
        # Persistent connections per OANDA best practices:
        # https://developer.oanda.com/rest-live-v20/best-practices/
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        })
        # Cumulative count of REST requests made through this client. Used by
        # FTMORiskGuard to enforce the 2,000-req/day FTMO ceiling. Streaming
        # connections don't increment this — they go through OandaStream.
        # Counter increments after the response is received (network errors
        # don't count — OANDA never saw them).
        self.request_count = 0
        self._count_lock = threading.Lock()

    def close(self):
        """Release the underlying connection pool. Call at shutdown."""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _url(self, path):
        return f"{self.base_url}/v3/accounts/{self.account_id}{path}"

    def _base_url(self, path):
        return f"{self.base_url}/v3{path}"

    def _check(self, resp):
        if not resp.ok:
            raise OandaAPIError(resp.status_code, resp.text)
        return resp.json()

    def _request(self, method, url, *, params=None, json=None, timeout=TIMEOUT_DEFAULT):
        """Send a request with one automatic retry on HTTP 429 (rate limited).

        Honors the Retry-After response header if present. No retry on POST
        beyond 429 — auto-retrying non-idempotent calls is dangerous because
        a timed-out order may have actually filled.
        """
        for attempt in range(2):
            # Increment AFTER the call completes — network errors that never
            # reached OANDA don't count against our daily budget.
            resp = self.session.request(
                method, url, params=params, json=json, timeout=timeout
            )
            with self._count_lock:
                self.request_count += 1
            if resp.status_code == 429 and attempt == 0:
                retry_after = resp.headers.get("Retry-After", "1")
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = 1  # HTTP-date form — just wait briefly
                time.sleep(min(wait, 30))
                continue
            return self._check(resp)

    def _get(self, path, params=None, use_base=False):
        url = self._base_url(path) if use_base else self._url(path)
        return self._request("GET", url, params=params)

    def _post(self, path, data, timeout=TIMEOUT_ORDER_POST):
        return self._request("POST", self._url(path), json=data, timeout=timeout)

    def _put(self, path, data):
        return self._request("PUT", self._url(path), json=data)

    def get_account_summary(self):
        return self._get("/summary")

    def get_candles(self, instrument, granularity="M1", count=10):
        return self._get(
            f"/instruments/{instrument}/candles",
            params={"granularity": granularity, "count": count},
            use_base=True,
        )

    def get_price(self, instrument):
        return self._get("/pricing", params={"instruments": instrument})

    def place_market_order(
        self,
        instrument,
        units,
        stop_loss_pips=None,
        take_profit_pips=None,
        price_bound=None,
        client_id=None,
        allow_naked=False,
    ):
        """Place a market order. Positive units = buy, negative = sell.

        SL is required by default (the Phase 3 "no naked positions" rule).
        To explicitly opt out (e.g., for closing trades that don't need SL),
        pass allow_naked=True.

        Risk controls:
        - stop_loss_pips: distance-form SL attached atomically with the fill.
          OANDA computes the SL price as `fill ± distance` server-side, so
          there's no race between the fill arriving and the SL being set.
        - take_profit_pips: distance-form TP. If set, the plan's 1:1.5
          minimum R:R is enforced.
        - price_bound: absolute price string. If the fill would happen at a
          worse price than this, OANDA rejects with PRICE_BOUNDS_VIOLATION
          rather than filling at the worse price (slippage cap).
        - client_id: idempotency key. Auto-generated if not provided. Allows
          `GET /orders/@client_id` lookup so a network-timed-out POST can be
          checked rather than blindly retried.

        Raises:
        - ValueError if SL is missing and allow_naked is False
        - ValueError if R:R ratio is below 1:1.5 when both SL and TP are set
        - OandaOrderRejected if OANDA returns 2xx with an
          orderRejectTransaction in the body (validation failure)

        Returns the raw response. Callers should check for `orderFillTransaction`
        (filled) vs `orderCancelTransaction` (FOK couldn't fill at requested price).
        """
        if stop_loss_pips is None and not allow_naked:
            raise ValueError(
                "stop_loss_pips is required (no naked positions). "
                "Pass allow_naked=True to explicitly override."
            )
        if stop_loss_pips is not None and take_profit_pips is not None:
            validate_risk_reward(stop_loss_pips, take_profit_pips)

        order = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "clientExtensions": {"id": client_id or generate_client_id()},
        }
        if stop_loss_pips is not None:
            order["stopLossOnFill"] = {
                "timeInForce": "GTC",
                "distance": pip_distance(instrument, stop_loss_pips),
            }
        if take_profit_pips is not None:
            order["takeProfitOnFill"] = {
                "timeInForce": "GTC",
                "distance": pip_distance(instrument, take_profit_pips),
            }
        if price_bound is not None:
            order["priceBound"] = str(price_bound)

        response = self._post("/orders", {"order": order})
        if "orderRejectTransaction" in response:
            raise OandaOrderRejected(response["orderRejectTransaction"])
        return response

    def get_open_positions(self):
        return self._get("/openPositions")

    def get_transactions(self, since_id="0"):
        """Get all transactions with ID greater than since_id.

        OANDA returns transactions strictly *after* since_id, so "0" gets
        every transaction including the account-creation event (ID 1).
        """
        return self._get("/transactions/sinceid", params={"id": str(since_id)})

    def close_position(self, instrument):
        """Close entire position for an instrument.

        OANDA rejects the request if you ask to close a side that doesn't exist,
        so we query the position first and only include the sides that have units.
        Raises OandaAPIError if the position query fails (instead of masking it).
        """
        position = self._get(f"/positions/{instrument}")
        if "position" not in position:
            raise ValueError(f"Unexpected position response shape: {position}")
        pos = position["position"]
        long_units = int(pos.get("long", {}).get("units", "0"))
        short_units = int(pos.get("short", {}).get("units", "0"))

        payload = {}
        if long_units > 0:
            payload["longUnits"] = "ALL"
        if short_units < 0:
            payload["shortUnits"] = "ALL"

        if not payload:
            return {"noPosition": True, "instrument": instrument}

        return self._put(f"/positions/{instrument}/close", payload)
