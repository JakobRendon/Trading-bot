import json
import time
import requests


class OandaStream:
    """Streaming price client for OANDA v20 API.

    Opens a long-lived HTTP connection to the streaming endpoint and dispatches
    PRICE and HEARTBEAT events to registered callbacks. Reconnects automatically
    with exponential backoff on connection errors.
    """

    def __init__(self, api_token, account_id, base_url):
        self.account_id = account_id
        # Streaming uses a different subdomain than REST: stream-* instead of api-*
        self.stream_url = base_url.replace("api-", "stream-")
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept-Datetime-Format": "RFC3339",
        }
        self.price_callbacks = []
        self.heartbeat_callbacks = []
        self._stop = False

    def on_price(self, callback):
        self.price_callbacks.append(callback)

    def on_heartbeat(self, callback):
        self.heartbeat_callbacks.append(callback)

    def stop(self):
        self._stop = True

    def start(self, instruments, max_reconnects=10):
        """Connect to the stream and dispatch events to callbacks. Blocks until stop()."""
        url = f"{self.stream_url}/v3/accounts/{self.account_id}/pricing/stream"
        params = {"instruments": ",".join(instruments)}

        self._stop = False
        backoff = 1
        reconnects = 0

        while not self._stop and reconnects <= max_reconnects:
            try:
                with requests.get(
                    url, headers=self.headers, params=params, stream=True, timeout=30
                ) as resp:
                    if not resp.ok:
                        print(f"Stream error {resp.status_code}: {resp.text}")
                        return

                    backoff = 1  # reset backoff on successful connect

                    for line in resp.iter_lines():
                        if self._stop:
                            return
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        event_type = data.get("type")
                        if event_type == "PRICE":
                            for cb in self.price_callbacks:
                                cb(data)
                        elif event_type == "HEARTBEAT":
                            for cb in self.heartbeat_callbacks:
                                cb(data)

                # Connection closed cleanly (no exception). Exit rather than
                # hammer the server with reconnects.
                return
            except (requests.ConnectionError, requests.Timeout) as e:
                if self._stop:
                    return
                print(f"Stream disconnected: {e}. Reconnecting in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                reconnects += 1
