import json
import os
from decimal import Decimal
from unittest.mock import MagicMock
import pytest

import risk_guard
from risk_guard import FTMORiskGuard


def make_mock_api(
    balance="10000.00",
    nav=None,
    open_positions=0,
    request_count=0,
    last_transaction_id="100",
    transactions=None,
):
    """Build a mock OandaAPI with configurable account state."""
    api = MagicMock()
    api.request_count = request_count

    def get_account_summary():
        return {
            "account": {
                "balance": balance,
                "NAV": nav if nav is not None else balance,
                "openPositionCount": open_positions,
                "lastTransactionID": last_transaction_id,
            }
        }
    api.get_account_summary = MagicMock(side_effect=get_account_summary)
    api.get_transactions = MagicMock(return_value={"transactions": transactions or []})
    return api


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "risk_state.json")


@pytest.fixture
def fixed_today(monkeypatch):
    """Pin _ftmo_today() to a single date so daily rollover doesn't fire."""
    monkeypatch.setattr(risk_guard, "_ftmo_today", lambda: "2026-05-25")
    # Pin midnight to a value before any test transaction time
    monkeypatch.setattr(
        risk_guard, "_ftmo_midnight_utc_iso", lambda: "2026-05-24T22:00:00Z"
    )
    return "2026-05-25"


@pytest.fixture
def fixed_monotonic(monkeypatch):
    """Pin time.monotonic to a controllable value."""
    values = {"now": 1000.0}
    monkeypatch.setattr(risk_guard.time, "monotonic", lambda: values["now"])
    return values


# --- Initialization & state ---

class TestInitialization:
    def test_first_init_creates_state_file(self, state_path, fixed_today):
        api = make_mock_api(balance="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        assert os.path.exists(state_path)

    def test_challenge_start_balance_from_constructor_persists(self, state_path, fixed_today):
        api = make_mock_api(balance="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        with open(state_path) as f:
            state = json.load(f)
        assert state["challenge_start_balance"] == "25000"

    def test_challenge_start_balance_snapshots_current_on_first_refresh(self, state_path, fixed_today):
        api = make_mock_api(balance="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path)
        guard.refresh()
        assert guard._state["challenge_start_balance"] == "25000.00"

    def test_existing_state_loaded(self, state_path, fixed_today):
        with open(state_path, "w") as f:
            json.dump({
                "challenge_start_balance": "50000.00",
                "daily_start_nav": "50000.00",
                "daily_start_date": "2026-05-25",
                "request_count_today": 0,
                "api_request_count_last_seen": 0,
                "request_baseline_date": "2026-05-25",
                "position_entries_today": 0,
                "position_entries_baseline_tx_id": "100",
                "position_entries_date": "2026-05-25",
            }, f)
        api = make_mock_api()
        guard = FTMORiskGuard(api, state_path=state_path)
        assert guard._state["challenge_start_balance"] == "50000.00"
        assert guard._state["daily_start_nav"] == "50000.00"

    def test_old_state_schema_migrates_daily_start_balance(self, state_path, fixed_today):
        """Old state files used daily_start_balance — migrate to daily_start_nav."""
        with open(state_path, "w") as f:
            json.dump({"daily_start_balance": "50000.00"}, f)
        api = make_mock_api()
        guard = FTMORiskGuard(api, state_path=state_path)
        assert guard._state["daily_start_nav"] == "50000.00"
        assert "daily_start_balance" not in guard._state

    def test_constructor_balance_overrides_persisted(self, state_path, fixed_today):
        with open(state_path, "w") as f:
            json.dump({"challenge_start_balance": "50000.00"}, f)
        api = make_mock_api()
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=100000)
        assert guard._state["challenge_start_balance"] == "100000"


# --- Daily P/L tracking ---

class TestDailyPL:
    def test_no_change_zero_pl(self, state_path, fixed_today):
        api = make_mock_api(balance="25000.00", nav="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        assert guard.daily_pl() == Decimal("0")
        assert guard.daily_loss_pct() == Decimal("0")

    def test_loss_calculated_correctly(self, state_path, fixed_today, fixed_monotonic):
        # Day starts with NAV 25000; then NAV drops to 24500 → 500 loss, 2% of 25000
        api = make_mock_api(balance="25000.00", nav="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        guard.refresh()  # anchors daily_start_nav at 25000

        api.get_account_summary.side_effect = lambda: {
            "account": {
                "balance": "25000.00", "NAV": "24500.00",
                "openPositionCount": 0, "lastTransactionID": "100",
            }
        }
        fixed_monotonic["now"] += 15  # bypass TTL
        assert guard.daily_pl() == Decimal("-500.00")
        assert guard.daily_loss_pct() == Decimal("2.00")

    def test_profit_returns_zero_loss(self, state_path, fixed_today, fixed_monotonic):
        api = make_mock_api(balance="25000.00", nav="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        guard.refresh()

        api.get_account_summary.side_effect = lambda: {
            "account": {
                "balance": "25000.00", "NAV": "26000.00",
                "openPositionCount": 0, "lastTransactionID": "100",
            }
        }
        fixed_monotonic["now"] += 15
        assert guard.daily_pl() == Decimal("1000.00")
        # Loss% is 0 when in profit
        assert guard.daily_loss_pct() == Decimal("0")


# --- Total drawdown ---

class TestTotalDrawdown:
    def test_zero_drawdown_when_at_start(self, state_path, fixed_today):
        api = make_mock_api(balance="25000.00", nav="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        assert guard.total_drawdown_pct() == Decimal("0")

    def test_drawdown_pct_calculation(self, state_path, fixed_today):
        api = make_mock_api(balance="25000.00", nav="22500.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        # (25000 - 22500) / 25000 = 10%
        assert guard.total_drawdown_pct() == Decimal("10.00")

    def test_above_challenge_start_returns_zero(self, state_path, fixed_today):
        api = make_mock_api(balance="25000.00", nav="30000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        assert guard.total_drawdown_pct() == Decimal("0")


# --- Enforcement: can_open_position ---

class TestCanOpenPosition:
    def test_allowed_when_all_metrics_healthy(self, state_path, fixed_today):
        api = make_mock_api(balance="25000.00", nav="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        allowed, reason = guard.can_open_position()
        assert allowed is True
        assert reason == ""

    def test_blocked_when_daily_loss_at_buffer(self, state_path, fixed_today, fixed_monotonic):
        # Day starts at NAV 25000, drops to 24000 = 4% loss
        api = make_mock_api(balance="25000.00", nav="25000.00")
        guard = FTMORiskGuard(
            api, state_path=state_path, challenge_start_balance=25000,
            daily_loss_buffer_pct=4,
        )
        guard.refresh()
        api.get_account_summary.side_effect = lambda: {
            "account": {
                "balance": "25000.00", "NAV": "24000.00",
                "openPositionCount": 0, "lastTransactionID": "100",
            }
        }
        fixed_monotonic["now"] += 15
        allowed, reason = guard.can_open_position()
        assert allowed is False
        assert "Daily loss" in reason

    def test_blocked_when_total_drawdown_at_buffer(self, state_path, fixed_today):
        # 9% of 25000 = 2250 drawdown → NAV = 22750
        api = make_mock_api(balance="25000.00", nav="22750.00")
        guard = FTMORiskGuard(
            api, state_path=state_path, challenge_start_balance=25000,
            daily_loss_buffer_pct=99,  # disable daily check so it doesn't fire first
            total_drawdown_buffer_pct=9,
        )
        allowed, reason = guard.can_open_position()
        assert allowed is False
        assert "Total drawdown" in reason

    def test_blocked_when_request_count_at_limit(self, state_path, fixed_today, fixed_monotonic):
        api = make_mock_api(balance="25000.00", request_count=0)
        guard = FTMORiskGuard(
            api, state_path=state_path, challenge_start_balance=25000,
            max_requests_per_day=1900,
        )
        guard.refresh()

        # Simulate 1900 requests made through the API
        api.request_count = 1900
        fixed_monotonic["now"] += 15  # bypass TTL cache

        allowed, reason = guard.can_open_position()
        assert allowed is False
        assert "API requests" in reason

    def test_blocked_when_open_positions_at_limit(self, state_path, fixed_today):
        api = make_mock_api(balance="25000.00", open_positions=180)
        guard = FTMORiskGuard(
            api, state_path=state_path, challenge_start_balance=25000,
            max_simultaneous_positions=180,
        )
        allowed, reason = guard.can_open_position()
        assert allowed is False
        assert "Open positions" in reason

    def test_blocked_when_position_entries_at_limit(self, state_path, fixed_today):
        api = make_mock_api(balance="25000.00")
        guard = FTMORiskGuard(
            api, state_path=state_path, challenge_start_balance=25000,
            max_position_entries_per_day=2,
        )
        guard.record_position_entry()
        guard.record_position_entry()
        allowed, reason = guard.can_open_position()
        assert allowed is False
        assert "position entries" in reason.lower()

    def test_just_under_buffer_allowed(self, state_path, fixed_today, fixed_monotonic):
        # 3.99% loss is under 4% buffer
        api = make_mock_api(balance="25000.00", nav="25000.00")
        guard = FTMORiskGuard(
            api, state_path=state_path, challenge_start_balance=25000,
            daily_loss_buffer_pct=4,
        )
        guard.refresh()
        # 25000 * 0.0399 = 997.5 loss → NAV = 24002.50
        api.get_account_summary.side_effect = lambda: {
            "account": {
                "balance": "25000.00", "NAV": "24002.50",
                "openPositionCount": 0, "lastTransactionID": "100",
            }
        }
        fixed_monotonic["now"] += 15
        allowed, reason = guard.can_open_position()
        assert allowed is True


# --- Daily rollover ---

class TestDailyRollover:
    def test_rollover_snapshots_new_daily_nav(self, state_path, monkeypatch):
        """Anchor is NAV (not balance) so carried unrealized P/L doesn't distort daily P/L."""
        # Day 1: NAV 25000
        monkeypatch.setattr(risk_guard, "_ftmo_today", lambda: "2026-05-25")
        api = make_mock_api(balance="25000.00", nav="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        guard.refresh()
        assert guard._state["daily_start_nav"] == "25000.00"
        assert guard._state["daily_start_date"] == "2026-05-25"

        # Day 2: balance is 25500 but NAV is 25800 (open position +300 unrealized)
        # Anchor should be NAV (25800), not balance (25500).
        monkeypatch.setattr(risk_guard, "_ftmo_today", lambda: "2026-05-26")
        api.get_account_summary.side_effect = lambda: {
            "account": {
                "balance": "25500.00",
                "NAV": "25800.00",
                "openPositionCount": 1,
                "lastTransactionID": "200",
            }
        }
        guard.invalidate_cache()
        guard.refresh()
        assert guard._state["daily_start_nav"] == "25800.00"
        assert guard._state["daily_start_date"] == "2026-05-26"

    def test_request_counter_accumulates_within_day(self, state_path, monkeypatch):
        monkeypatch.setattr(risk_guard, "_ftmo_today", lambda: "2026-05-25")
        api = make_mock_api(balance="25000.00", request_count=500)
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        guard.refresh()
        # First refresh: today's count starts at the initial api.request_count
        assert guard.daily_request_count() == 500

        # Same day, more requests
        api.request_count = 750
        guard.invalidate_cache()
        assert guard.daily_request_count() == 750

        # Rollover — counter resets
        monkeypatch.setattr(risk_guard, "_ftmo_today", lambda: "2026-05-26")
        api.request_count = 800
        guard.invalidate_cache()
        guard.refresh()
        assert guard.daily_request_count() == 50  # 800 - 750 (delta since last seen)

    def test_request_counter_survives_api_restart(self, state_path, monkeypatch):
        """When the bot restarts, api.request_count resets to 0 but persisted
        state preserves today's accumulated count."""
        monkeypatch.setattr(risk_guard, "_ftmo_today", lambda: "2026-05-25")
        api = make_mock_api(balance="25000.00", request_count=100)
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        guard.refresh()
        assert guard.daily_request_count() == 100

        # Simulate process restart: new API instance, count = 0
        api2 = make_mock_api(balance="25000.00", request_count=0)
        guard2 = FTMORiskGuard(api2, state_path=state_path)
        guard2.refresh()
        # Today's count preserved from disk, but no new requests in this run yet
        assert guard2.daily_request_count() == 100

        # New requests in this run accumulate
        api2.request_count = 50
        guard2.invalidate_cache()
        assert guard2.daily_request_count() == 150  # 100 (persisted) + 50 (new)

    def test_position_entries_resets_on_rollover(self, state_path, monkeypatch):
        monkeypatch.setattr(risk_guard, "_ftmo_today", lambda: "2026-05-25")
        api = make_mock_api(balance="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        guard.record_position_entry()
        guard.record_position_entry()
        assert guard.daily_position_entries() == 2

        monkeypatch.setattr(risk_guard, "_ftmo_today", lambda: "2026-05-26")
        guard.invalidate_cache()
        guard.refresh()
        assert guard.daily_position_entries() == 0

    def test_startup_reconciles_entries_from_transactions(self, state_path, fixed_today):
        """On startup, count entries from OANDA transactions, not from cold state."""
        today_time = "2026-05-25T12:00:00Z"
        yesterday_time = "2026-05-24T18:00:00Z"  # before midnight Prague
        api = make_mock_api(
            balance="25000.00",
            transactions=[
                {"type": "ORDER_FILL", "time": today_time, "tradesOpened": [{"tradeID": "1"}]},
                {"type": "ORDER_FILL", "time": today_time, "tradesOpened": [{"tradeID": "2"}]},
                # Close transaction today — should NOT count
                {"type": "ORDER_FILL", "time": today_time, "tradesClosed": [{"tradeID": "1"}]},
                {"type": "ORDER_FILL", "time": today_time, "tradesOpened": [{"tradeID": "3"}]},
                # Entry from yesterday — should NOT count
                {"type": "ORDER_FILL", "time": yesterday_time, "tradesOpened": [{"tradeID": "0"}]},
            ],
        )
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        guard.refresh()
        assert guard.daily_position_entries() == 3


# --- TTL caching ---

class TestRefreshTTL:
    def test_caches_within_ttl(self, state_path, fixed_today, fixed_monotonic):
        api = make_mock_api(balance="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000,
                              refresh_ttl_seconds=10)
        guard.refresh()
        initial_calls = api.get_account_summary.call_count

        # Within TTL — should NOT re-fetch
        fixed_monotonic["now"] += 5
        guard.daily_pl()
        assert api.get_account_summary.call_count == initial_calls

    def test_refreshes_after_ttl(self, state_path, fixed_today, fixed_monotonic):
        api = make_mock_api(balance="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000,
                              refresh_ttl_seconds=10)
        guard.refresh()
        initial_calls = api.get_account_summary.call_count

        fixed_monotonic["now"] += 15  # past TTL
        guard.daily_pl()
        assert api.get_account_summary.call_count > initial_calls

    def test_explicit_refresh_bypasses_ttl(self, state_path, fixed_today, fixed_monotonic):
        api = make_mock_api(balance="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        guard.refresh()
        initial_calls = api.get_account_summary.call_count
        guard.refresh()  # explicit refresh, no time passed
        assert api.get_account_summary.call_count > initial_calls


# --- Persistence ---

class TestPersistence:
    def test_state_survives_reinitialization(self, state_path, fixed_today):
        """Persisted state + matching OANDA transactions = same entry count after restart."""
        today_time = "2026-05-25T12:00:00Z"
        transactions = [
            {"type": "ORDER_FILL", "time": today_time, "tradesOpened": [{"tradeID": "1"}]},
            {"type": "ORDER_FILL", "time": today_time, "tradesOpened": [{"tradeID": "2"}]},
        ]
        api = make_mock_api(balance="25000.00", transactions=transactions)
        guard1 = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        guard1.record_position_entry()
        guard1.record_position_entry()

        # New guard with same state path — reconciles from transactions
        api2 = make_mock_api(balance="25000.00", transactions=transactions)
        guard2 = FTMORiskGuard(api2, state_path=state_path)
        assert guard2._state["challenge_start_balance"] == "25000"
        assert guard2.daily_position_entries() == 2  # reconciled from OANDA

    def test_state_with_missing_keys_backfilled(self, state_path, fixed_today):
        """Forward-compatible: missing keys get default values, no crash."""
        with open(state_path, "w") as f:
            json.dump({"challenge_start_balance": "25000"}, f)
        api = make_mock_api(balance="25000.00")
        # Should not raise — backfilled
        guard = FTMORiskGuard(api, state_path=state_path)
        assert guard._state["position_entries_today"] == 0

    def test_truncated_json_state_file_recovered(self, state_path, fixed_today):
        """Truncated/invalid JSON moved aside; fresh state initialized."""
        with open(state_path, "w") as f:
            f.write('{"challenge_start_')  # truncated
        api = make_mock_api(balance="25000.00")
        # Should not raise — corrupt file moved aside
        guard = FTMORiskGuard(api, state_path=state_path)
        # State directory should now contain a .corrupt-* backup
        corrupt_files = [
            f for f in os.listdir(os.path.dirname(state_path) or ".")
            if f.startswith(os.path.basename(state_path)) and ".corrupt-" in f
        ]
        assert len(corrupt_files) >= 1

    def test_nan_in_state_file_rejected(self, state_path, fixed_today):
        """NaN in challenge_start_balance must raise — NaN comparisons silently
        return False and would disable the drawdown guard."""
        with open(state_path, "w") as f:
            json.dump({"challenge_start_balance": "NaN"}, f)
        api = make_mock_api(balance="25000.00")
        with pytest.raises(ValueError, match="finite"):
            FTMORiskGuard(api, state_path=state_path)

    def test_infinity_in_state_file_rejected(self, state_path, fixed_today):
        with open(state_path, "w") as f:
            json.dump({"challenge_start_balance": "Infinity"}, f)
        api = make_mock_api(balance="25000.00")
        with pytest.raises(ValueError, match="finite"):
            FTMORiskGuard(api, state_path=state_path)


# --- Fail-closed on refresh failure ---

class TestFailClosed:
    def test_refresh_failure_after_initial_success_keeps_cached_state(
        self, state_path, fixed_today, fixed_monotonic
    ):
        api = make_mock_api(balance="25000.00", nav="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        guard.refresh()  # initial success

        # Now make subsequent calls fail
        api.get_account_summary.side_effect = ConnectionError("network down")
        fixed_monotonic["now"] += 15  # past TTL → forces refresh

        # Should NOT raise — keep cached state, set _refresh_error
        guard.refresh()
        assert guard._refresh_error is not None

    def test_can_open_position_blocks_when_refresh_failing(
        self, state_path, fixed_today, fixed_monotonic
    ):
        api = make_mock_api(balance="25000.00", nav="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        guard.refresh()

        # Subsequent refresh fails
        api.get_account_summary.side_effect = ConnectionError("network down")
        fixed_monotonic["now"] += 15

        allowed, reason = guard.can_open_position()
        assert allowed is False
        assert "verify account state" in reason

    def test_initial_refresh_failure_raises(self, state_path, fixed_today):
        """Never-successful refresh: can't proceed at all."""
        api = make_mock_api()
        api.get_account_summary.side_effect = ConnectionError("network down")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        with pytest.raises(ConnectionError):
            guard.refresh()


# --- Warnings ---

class TestWarnings:
    def test_no_warnings_when_under_threshold(self, state_path, fixed_today):
        api = make_mock_api(balance="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        assert guard.warnings() == []

    def test_warning_at_1500_requests(self, state_path, fixed_today, fixed_monotonic):
        api = make_mock_api(balance="25000.00", request_count=0)
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        guard.refresh()

        api.request_count = 1500
        fixed_monotonic["now"] += 15
        warnings = guard.warnings()
        assert any("approaching limit" in w for w in warnings)


# --- Summary ---

class TestSummary:
    def test_summary_has_all_fields(self, state_path, fixed_today):
        api = make_mock_api(balance="25000.00")
        guard = FTMORiskGuard(api, state_path=state_path, challenge_start_balance=25000)
        s = guard.summary()
        for key in (
            "challenge_start_balance", "current_nav", "daily_pl",
            "daily_loss_pct", "total_drawdown_pct",
            "daily_requests", "max_requests_per_day",
            "daily_entries", "open_positions",
        ):
            assert key in s
