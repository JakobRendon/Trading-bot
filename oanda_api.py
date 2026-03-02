import requests


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

    def _get(self, path, params=None):
        resp = requests.get(self._url(path), headers=self.headers, params=params)
        if not resp.ok:
            print(f"ERROR {resp.status_code}: {resp.text}")
        return resp.json()

    def _post(self, path, data):
        resp = requests.post(self._url(path), headers=self.headers, json=data)
        if not resp.ok:
            print(f"ERROR {resp.status_code}: {resp.text}")
        return resp.json()

    def _put(self, path, data):
        resp = requests.put(self._url(path), headers=self.headers, json=data)
        if not resp.ok:
            print(f"ERROR {resp.status_code}: {resp.text}")
        return resp.json()

    def get_account_summary(self):
        return self._get("/summary")

    def get_candles(self, instrument, granularity="M1", count=10):
        return self._get(
            f"/instruments/{instrument}/candles",
            params={"granularity": granularity, "count": count},
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
            }
        }
        return self._post("/orders", data)

    def get_open_positions(self):
        return self._get("/openPositions")

    def close_position(self, instrument):
        """Close entire position for an instrument (both long and short)."""
        return self._put(
            f"/positions/{instrument}/close",
            {"longUnits": "ALL", "shortUnits": "ALL"},
        )
