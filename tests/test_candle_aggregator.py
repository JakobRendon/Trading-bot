from decimal import Decimal
import pytest
from candle_aggregator import (
    CandleAggregator,
    parse_granularity,
    bucket_start,
    parse_tick_time,
    mid_price,
)


def make_tick(time_str, bid, ask, instrument="EUR_USD"):
    return {
        "type": "PRICE",
        "time": time_str,
        "instrument": instrument,
        "bids": [{"price": str(bid)}],
        "asks": [{"price": str(ask)}],
    }


# --- Helper function tests ---

class TestGranularityParsing:
    def test_m1_is_60_seconds(self):
        assert parse_granularity("M1") == 60

    def test_m5_is_300_seconds(self):
        assert parse_granularity("M5") == 300

    def test_h1_is_3600_seconds(self):
        assert parse_granularity("H1") == 3600

    def test_h4_is_14400_seconds(self):
        assert parse_granularity("H4") == 14400

    def test_d_is_86400_seconds(self):
        assert parse_granularity("D") == 86400

    def test_invalid_granularity_raises(self):
        with pytest.raises(ValueError):
            parse_granularity("XYZ")


class TestBucketStart:
    def test_floors_to_bucket_boundary(self):
        # Bucket size 300 (M5). Timestamp 1000 → bucket starts at 900.
        assert bucket_start(1000, 300) == 900

    def test_exact_boundary_returns_itself(self):
        assert bucket_start(900, 300) == 900

    def test_floors_to_hour_boundary(self):
        # 3661 (1h 0m 1s) with H1 → 3600
        assert bucket_start(3661, 3600) == 3600


class TestParseTickTime:
    def test_handles_z_suffix(self):
        ts = parse_tick_time("2026-05-25T00:00:00Z")
        assert ts == 1779667200.0  # 2026-05-25T00:00:00 UTC

    def test_handles_nanosecond_precision(self):
        ts = parse_tick_time("2026-05-25T00:00:00.090717491Z")
        assert ts == pytest.approx(1779667200.090717, abs=0.001)

    def test_two_times_one_minute_apart(self):
        t1 = parse_tick_time("2026-05-25T00:00:00Z")
        t2 = parse_tick_time("2026-05-25T00:01:00Z")
        assert t2 - t1 == 60


class TestMidPrice:
    def test_mid_of_bid_and_ask(self):
        tick = make_tick("2026-05-25T00:00:00Z", "1.10000", "1.10010")
        assert mid_price(tick) == Decimal("1.10005")

    def test_empty_bids_returns_none(self):
        tick = {
            "type": "PRICE",
            "time": "2026-05-25T00:00:00Z",
            "instrument": "EUR_USD",
            "bids": [],
            "asks": [{"price": "1.10010"}],
        }
        assert mid_price(tick) is None

    def test_empty_asks_returns_none(self):
        tick = {
            "type": "PRICE",
            "time": "2026-05-25T00:00:00Z",
            "instrument": "EUR_USD",
            "bids": [{"price": "1.10000"}],
            "asks": [],
        }
        assert mid_price(tick) is None

    def test_missing_bids_key_returns_none(self):
        tick = {"type": "PRICE", "asks": [{"price": "1.0"}]}
        assert mid_price(tick) is None


class TestAggregatorEmptyQuotes:
    def test_tick_with_empty_bids_is_skipped(self):
        from candle_aggregator import CandleAggregator
        agg = CandleAggregator(["M5"])
        agg.on_tick({
            "type": "PRICE",
            "time": "2026-05-25T00:00:00Z",
            "instrument": "EUR_USD",
            "bids": [],
            "asks": [{"price": "1.10010"}],
        })
        # No candle should have been created for this halted-instrument tick
        assert agg._current == {}


# --- Aggregator behavior ---

class TestAggregatorSingleGranularity:
    def test_first_tick_opens_candle_without_emitting(self):
        agg = CandleAggregator(["M5"])
        closed = []
        agg.on_candle_close(lambda g, c: closed.append((g, c)))

        agg.on_tick(make_tick("2026-05-25T00:00:00Z", "1.10000", "1.10010"))

        assert len(closed) == 0
        assert ("EUR_USD", "M5") in agg._current

    def test_ticks_in_same_bucket_update_hlc_and_volume(self):
        agg = CandleAggregator(["M5"])
        closed = []
        agg.on_candle_close(lambda g, c: closed.append((g, c)))

        agg.on_tick(make_tick("2026-05-25T00:00:00Z", "1.10000", "1.10010"))  # mid 1.10005
        agg.on_tick(make_tick("2026-05-25T00:01:00Z", "1.10020", "1.10030"))  # mid 1.10025
        agg.on_tick(make_tick("2026-05-25T00:02:00Z", "1.09990", "1.10000"))  # mid 1.09995
        agg.on_tick(make_tick("2026-05-25T00:03:00Z", "1.10010", "1.10020"))  # mid 1.10015

        assert len(closed) == 0  # still inside the same M5 bucket
        candle = agg._current[("EUR_USD", "M5")]
        assert candle["open"] == Decimal("1.10005")
        assert candle["high"] == Decimal("1.10025")
        assert candle["low"] == Decimal("1.09995")
        assert candle["close"] == Decimal("1.10015")
        assert candle["volume"] == 4

    def test_tick_in_new_bucket_emits_close_and_starts_new_candle(self):
        agg = CandleAggregator(["M5"])
        closed = []
        agg.on_candle_close(lambda g, c: closed.append((g, c)))

        agg.on_tick(make_tick("2026-05-25T00:00:00Z", "1.10000", "1.10010"))
        agg.on_tick(make_tick("2026-05-25T00:05:00Z", "1.10020", "1.10030"))  # new M5 bucket

        assert len(closed) == 1
        granularity, candle = closed[0]
        assert granularity == "M5"
        assert candle["open"] == Decimal("1.10005")
        assert candle["close"] == Decimal("1.10005")  # only one tick in first bucket
        assert candle["volume"] == 1
        # New candle started
        new_candle = agg._current[("EUR_USD", "M5")]
        assert new_candle["open"] == Decimal("1.10025")

    def test_stale_tick_is_ignored(self):
        agg = CandleAggregator(["M5"])

        agg.on_tick(make_tick("2026-05-25T00:05:00Z", "1.10000", "1.10010"))
        # Tick from an earlier bucket — should be ignored
        agg.on_tick(make_tick("2026-05-25T00:00:00Z", "1.50000", "1.50010"))

        candle = agg._current[("EUR_USD", "M5")]
        # The stale tick should NOT have affected high/low/close
        assert candle["high"] == Decimal("1.10005")
        assert candle["volume"] == 1

    def test_non_price_events_ignored(self):
        agg = CandleAggregator(["M5"])
        agg.on_tick({"type": "HEARTBEAT", "time": "2026-05-25T00:00:00Z"})
        assert agg._current == {}


class TestAggregatorMultipleGranularities:
    def test_tracks_independent_candles_per_granularity(self):
        agg = CandleAggregator(["M1", "M5"])

        agg.on_tick(make_tick("2026-05-25T00:00:00Z", "1.10000", "1.10010"))
        agg.on_tick(make_tick("2026-05-25T00:01:00Z", "1.10020", "1.10030"))

        m1 = agg._current[("EUR_USD", "M1")]
        m5 = agg._current[("EUR_USD", "M5")]
        # M1 candle should have rolled over (only the second tick)
        assert m1["volume"] == 1
        assert m1["open"] == Decimal("1.10025")
        # M5 candle should still have both ticks
        assert m5["volume"] == 2

    def test_m1_closes_while_m5_does_not(self):
        agg = CandleAggregator(["M1", "M5"])
        closed = []
        agg.on_candle_close(lambda g, c: closed.append((g, c["volume"])))

        agg.on_tick(make_tick("2026-05-25T00:00:00Z", "1.10000", "1.10010"))
        agg.on_tick(make_tick("2026-05-25T00:01:00Z", "1.10020", "1.10030"))

        granularities_closed = [g for g, _ in closed]
        assert "M1" in granularities_closed
        assert "M5" not in granularities_closed


class TestAggregatorMultipleInstruments:
    def test_independent_state_per_instrument(self):
        agg = CandleAggregator(["M5"])

        agg.on_tick(make_tick("2026-05-25T00:00:00Z", "1.10000", "1.10010", instrument="EUR_USD"))
        agg.on_tick(make_tick("2026-05-25T00:00:00Z", "1.26000", "1.26010", instrument="GBP_USD"))

        eur = agg._current[("EUR_USD", "M5")]
        gbp = agg._current[("GBP_USD", "M5")]
        assert eur["open"] == Decimal("1.10005")
        assert gbp["open"] == Decimal("1.26005")


class TestAggregatorValidation:
    def test_invalid_granularity_raises_at_construction(self):
        with pytest.raises(ValueError):
            CandleAggregator(["BAD"])


class TestCallbacks:
    def test_multiple_callbacks_all_fire(self):
        agg = CandleAggregator(["M1"])
        cb1_calls = []
        cb2_calls = []
        agg.on_candle_close(lambda g, c: cb1_calls.append(c))
        agg.on_candle_close(lambda g, c: cb2_calls.append(c))

        agg.on_tick(make_tick("2026-05-25T00:00:00Z", "1.10000", "1.10010"))
        agg.on_tick(make_tick("2026-05-25T00:01:00Z", "1.10020", "1.10030"))

        assert len(cb1_calls) == 1
        assert len(cb2_calls) == 1

    def test_no_callback_registered_does_not_crash(self):
        agg = CandleAggregator(["M1"])
        agg.on_tick(make_tick("2026-05-25T00:00:00Z", "1.10000", "1.10010"))
        agg.on_tick(make_tick("2026-05-25T00:01:00Z", "1.10020", "1.10030"))
        # Should not raise
