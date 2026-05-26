import time
import requests


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
            resp = self.session.request(
                method, url, params=params, json=json, timeout=timeout
            )
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

    def place_market_order(self, instrument, units):
        """Place a market order. Positive units = buy, negative = sell.

        Raises OandaOrderRejected if OANDA returns 2xx with an
        orderRejectTransaction in the body (validation failure).

        Returns the raw response. Callers should check for `orderFillTransaction`
        (filled) vs `orderCancelTransaction` (FOK couldn't fill at requested price).
        """
        data = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
            }
        }
        response = self._post("/orders", data)
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
