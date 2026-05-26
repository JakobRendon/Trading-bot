import requests


class OandaAPIError(Exception):
    """Raised when the OANDA REST API returns a non-2xx response."""

    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"OANDA API {status_code}: {body}")


class OandaAPI:
    def __init__(self, api_token, account_id, base_url):
        self.account_id = account_id
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    def _url(self, path):
        return f"{self.base_url}/v3/accounts/{self.account_id}{path}"

    def _base_url(self, path):
        return f"{self.base_url}/v3{path}"

    def _check(self, resp):
        if not resp.ok:
            raise OandaAPIError(resp.status_code, resp.text)
        return resp.json()

    def _get(self, path, params=None, use_base=False):
        url = self._base_url(path) if use_base else self._url(path)
        resp = requests.get(url, headers=self.headers, params=params, timeout=10)
        return self._check(resp)

    def _post(self, path, data):
        resp = requests.post(self._url(path), headers=self.headers, json=data, timeout=10)
        return self._check(resp)

    def _put(self, path, data):
        resp = requests.put(self._url(path), headers=self.headers, json=data, timeout=10)
        return self._check(resp)

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
        """Place a market order. Positive units = buy, negative = sell."""
        data = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
            }
        }
        return self._post("/orders", data)

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
