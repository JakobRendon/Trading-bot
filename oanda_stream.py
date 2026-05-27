import json
import logging
import time
import requests

logger = logging.getLogger(__name__)


# OANDA sends heartbeats every 5 seconds. A 10s socket timeout doubles that
# interval, so a stalled feed (no price OR heartbeat) is detected within 10s
# and triggers a reconnect via the requests.Timeout handler.
HEARTBEAT_TIMEOUT = 10


class OandaStream:
    """Streaming price client for OANDA v20 API.

    Opens a long-lived HTTP connection to the streaming endpoint and dispatches
    PRICE and HEARTBEAT events to registered callbacks. Reconnects indefinitely
    with exponential backoff on connection errors, 5xx responses, and clean
    disconnects. Gives up only on 4xx (auth/client errors that won't fix themselves).
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

    def start(self, instruments, max_reconnects=None):
        """Connect to the stream and dispatch events to callbacks. Blocks until stop().

        max_reconnects=None means retry forever. Production bots should leave this
        as None; tests can pass a small number to bound retry duration.
        """
        url = f"{self.stream_url}/v3/accounts/{self.account_id}/pricing/stream"
        params = {"instruments": ",".join(instruments)}

        self._stop = False
        backoff = 1
        reconnects = 0

        while not self._stop:
            if max_reconnects is not None and reconnects > max_reconnects:
                print(f"Stream giving up after {reconnects} reconnect attempts")
                return

            received_data = False
            try:
                with requests.get(
                    url,
                    headers=self.headers,
                    params=params,
                    stream=True,
                    timeout=HEARTBEAT_TIMEOUT,
                ) as resp:
                    if not resp.ok:
                        print(f"Stream error {resp.status_code}: {resp.text}")
                        if 400 <= resp.status_code < 500:
                            # Auth/client errors won't fix themselves
                            return
                        # 5xx — fall through to backoff and retry
                    else:
                        for line in resp.iter_lines():
                            if self._stop:
                                return
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                            except json.JSONDecodeError:
                                continue

                            received_data = True
                            event_type = data.get("type")
                            # Isolate per-callback exceptions: a single bad
                            # callback must not kill the iter_lines loop,
                            # because that would silently take down every
                            # other downstream consumer of the stream.
                            if event_type == "PRICE":
                                for cb in self.price_callbacks:
                                    try:
                                        cb(data)
                                    except Exception:
                                        logger.exception(
                                            "Price callback raised; continuing"
                                        )
                            elif event_type == "HEARTBEAT":
                                for cb in self.heartbeat_callbacks:
                                    try:
                                        cb(data)
                                    except Exception:
                                        logger.exception(
                                            "Heartbeat callback raised; continuing"
                                        )

                        # iter_lines exited without exception — server closed
                        # the connection cleanly. Reconnect with backoff.
                        if not self._stop:
                            print("Stream closed by server, reconnecting...")
            except (requests.ConnectionError, requests.Timeout) as e:
                if self._stop:
                    return
                print(f"Stream disconnected: {e}")

            if self._stop:
                return

            # Reset backoff/reconnect counter if we got real data — transient
            # disconnects shouldn't accumulate toward the give-up threshold.
            if received_data:
                backoff = 1
                reconnects = 0

            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            reconnects += 1
