import logging
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger(__name__)


GRANULARITY_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D": 86400,
}


def parse_granularity(granularity):
    if granularity not in GRANULARITY_SECONDS:
        raise ValueError(
            f"Unsupported granularity '{granularity}'. "
            f"Use one of: {sorted(GRANULARITY_SECONDS)}"
        )
    return GRANULARITY_SECONDS[granularity]


def bucket_start(epoch_seconds, bucket_size):
    """Floor a timestamp to the nearest bucket boundary."""
    return (epoch_seconds // bucket_size) * bucket_size


def parse_tick_time(time_str):
    """Parse an RFC3339 timestamp into epoch seconds (UTC)."""
    # OANDA times come as "2026-05-25T21:24:13.090717491Z" — Python <3.11 doesn't
    # accept the trailing Z, and the nanosecond precision needs trimming.
    cleaned = time_str.rstrip("Z")
    if "." in cleaned:
        date_part, frac = cleaned.split(".", 1)
        frac = frac[:6]  # microsecond precision
        cleaned = f"{date_part}.{frac}"
    dt = datetime.fromisoformat(cleaned).replace(tzinfo=timezone.utc)
    return dt.timestamp()


def mid_price(tick):
    """Compute mid price from the top-of-book bid and ask.

    Returns None if bids or asks are empty — OANDA sends PRICE events with
    empty arrays for halted/closed instruments, which would otherwise crash
    the stream.
    """
    bids = tick.get("bids") or []
    asks = tick.get("asks") or []
    if not bids or not asks:
        return None
    bid = Decimal(bids[0]["price"])
    ask = Decimal(asks[0]["price"])
    return (bid + ask) / 2


class CandleAggregator:
    """Builds candles locally from price ticks for one or more granularities.

    Wire it to an OandaStream like this:
        agg = CandleAggregator(["M5", "M15", "H1", "H4"])
        agg.on_candle_close(lambda granularity, candle: ...)
        stream.on_price(agg.on_tick)

    Note: "volume" is the count of ticks within the bucket, not OANDA's
    candle volume (which counts trades). Use it as a relative tick-density signal.
    """

    def __init__(self, granularities):
        self.granularities = list(granularities)
        for g in self.granularities:
            parse_granularity(g)  # validate
        self._current = {}  # key: (instrument, granularity), value: current candle dict
        self._callbacks = []

    def on_candle_close(self, callback):
        """Register a callback that receives (granularity, candle) on candle close."""
        self._callbacks.append(callback)

    def on_tick(self, tick):
        """Feed a PRICE event from OandaStream into the aggregator."""
        if tick.get("type") != "PRICE":
            return
        price = mid_price(tick)
        if price is None:
            return  # halted/no-quote tick — nothing to aggregate
        instrument = tick["instrument"]
        timestamp = parse_tick_time(tick["time"])

        for granularity in self.granularities:
            bucket_size = parse_granularity(granularity)
            start = bucket_start(timestamp, bucket_size)
            key = (instrument, granularity)
            current = self._current.get(key)

            if current is None:
                self._current[key] = self._new_candle(instrument, granularity, start, price)
                continue

            if start == current["start_time"]:
                # Tick belongs to the current candle — update H/L/C and volume
                if price > current["high"]:
                    current["high"] = price
                if price < current["low"]:
                    current["low"] = price
                current["close"] = price
                current["volume"] += 1
            elif start > current["start_time"]:
                # Tick belongs to a new bucket — close the old candle and start a new one
                self._emit_close(current)
                self._current[key] = self._new_candle(instrument, granularity, start, price)
            # If start < current["start_time"], the tick is stale — ignore it

    def _new_candle(self, instrument, granularity, start_time, price):
        return {
            "instrument": instrument,
            "granularity": granularity,
            "start_time": start_time,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1,
        }

    def _emit_close(self, candle):
        # Isolate per-callback exceptions: a bug in one consumer (e.g. a
        # strategy runner) must not prevent other registered callbacks
        # from seeing the candle close.
        for cb in self._callbacks:
            try:
                cb(candle["granularity"], candle)
            except Exception:
                logger.exception("Candle-close callback raised; continuing")
