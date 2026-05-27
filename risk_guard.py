"""
FTMO compliance tracking layer.

FTMORiskGuard reads account state from OandaAPI and tracks limits derived from
FTMO's rules + OANDA's per-IP API ceilings. Strategy code consults the guard
before submitting orders:

    allowed, reason = guard.can_open_position()
    if not allowed:
        log.warning("Skipping signal: %s", reason)
        return

The guard caches account state for a configurable TTL (default 10s). It persists
across restarts via JSON so the daily NAV anchor and challenge start balance
survive crashes. State writes are atomic (write-temp-then-rename).

Limits enforced (all buffer values configurable):
- Daily loss: stops at 4% (FTMO hard limit is 5%)
- Total drawdown: stops at 9% (FTMO hard limit is 10%)
- API requests per day: warns at 1,500, stops at 1,900 (FTMO ceiling is 2,000)
- Position entries per day: stops at 1,900 (FTMO ceiling is 2,000)
- Simultaneous open positions: stops at 180 (FTMO ceiling is 200)

Position-entry counting:
- The local counter increments on record_position_entry() and is the primary
  source of truth between refreshes.
- On daily rollover (and on startup), the counter is reconciled against
  OANDA transactions to correct any drift from crashes between fill and
  record_position_entry().
"""

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


FTMO_TIMEZONE = ZoneInfo("Europe/Prague")
REQUEST_WARNING_THRESHOLD = 1500


def _ftmo_today():
    """Current date in FTMO's reset timezone (Europe/Prague)."""
    return datetime.now(FTMO_TIMEZONE).date().isoformat()


def _ftmo_midnight_utc_iso():
    """ISO-formatted UTC timestamp for the most recent FTMO midnight."""
    today_local = datetime.now(FTMO_TIMEZONE).date()
    midnight_local = datetime.combine(
        today_local, datetime.min.time(), tzinfo=FTMO_TIMEZONE
    )
    return midnight_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path, data):
    """Write JSON atomically: temp file + rename so a crash mid-write doesn't corrupt state."""
    dir_path = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".risk_state_", suffix=".tmp", dir=dir_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _validated_decimal_str(value, field_name):
    """Coerce a stored value to a finite-Decimal string. Reject NaN/Infinity.

    NaN comparisons silently return False — `Decimal("NaN") >= 9` is False, which
    would silently disable the drawdown guard. We reject these explicitly.
    """
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"Invalid {field_name}: {value!r}") from e
    if not d.is_finite():
        raise ValueError(f"{field_name} must be a finite number, got: {value!r}")
    return str(d)


class FTMORiskGuard:
    def __init__(
        self,
        api,
        state_path="risk_state.json",
        challenge_start_balance=None,
        challenge_type="2-step",
        daily_loss_buffer_pct=4,
        total_drawdown_buffer_pct=9,
        max_requests_per_day=1900,
        max_position_entries_per_day=1900,
        max_simultaneous_positions=180,
        refresh_ttl_seconds=10,
    ):
        if challenge_type not in ("1-step", "2-step"):
            raise ValueError(
                f"challenge_type must be '1-step' or '2-step', got: {challenge_type!r}"
            )
        self.api = api
        self.state_path = state_path
        self.challenge_type = challenge_type
        self.daily_loss_buffer_pct = Decimal(str(daily_loss_buffer_pct))
        self.total_drawdown_buffer_pct = Decimal(str(total_drawdown_buffer_pct))
        self.max_requests_per_day = max_requests_per_day
        self.max_position_entries_per_day = max_position_entries_per_day
        self.max_simultaneous_positions = max_simultaneous_positions
        self.refresh_ttl_seconds = refresh_ttl_seconds

        # Cached account snapshot from most recent refresh
        self._balance = None
        self._nav = None
        self._open_position_count = 0
        self._last_refresh_monotonic = None
        # Error from most recent refresh attempt (None if last attempt succeeded)
        self._refresh_error = None
        # Reconciliation flag — when False, the next refresh will re-derive
        # position_entries_today from OANDA transactions (catches entries that
        # were filled but missed by record_position_entry due to a crash).
        self._reconciled_in_this_process = False

        # RLock — public methods that nest (can_open_position calls daily_loss_pct
        # which calls _ensure_fresh) need re-entrant acquisition.
        self._lock = threading.RLock()

        # Persistent state
        self._state = self._load_state()

        # If no challenge_start_balance was set and none was provided, snapshot
        # the current account balance on first refresh. Otherwise honor the
        # provided value, overwriting any persisted value.
        if challenge_start_balance is not None:
            self._state["challenge_start_balance"] = _validated_decimal_str(
                challenge_start_balance, "challenge_start_balance"
            )
            self._save_state()

    # ---- state persistence ----

    def _default_state(self):
        return {
            "schema_version": 1,
            "challenge_start_balance": None,
            # FTMO's Max Daily Loss is measured as Equity vs the BALANCE
            # recorded at 00:00 CE(S)T (not NAV). See FTMO MDL FAQ. A prior
            # iteration anchored on NAV; this was less conservative than
            # FTMO's actual rule and could miss the hard limit.
            "daily_start_balance": None,
            "daily_start_date": None,
            "request_count_today": 0,
            "api_request_count_last_seen": 0,
            "request_baseline_date": None,
            "position_entries_today": 0,
            "position_entries_date": None,
        }

    def _load_state(self):
        defaults = self._default_state()
        if not os.path.exists(self.state_path):
            return defaults
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            # Corrupted state file — preserve evidence for debugging and start fresh.
            backup = f"{self.state_path}.corrupt-{int(time.time())}"
            try:
                os.replace(self.state_path, backup)
            except OSError:
                pass
            print(f"Risk state file corrupted ({e}); moved to {backup}, starting fresh")
            return defaults
        # Backfill missing keys (forward-compatibility) and migrate old key names.
        # daily_start_nav was the (incorrect) anchor during a brief window;
        # transparently rename it. The next daily rollover re-anchors to
        # balance, so any short-term inaccuracy self-heals within 24h.
        if "daily_start_nav" in state and "daily_start_balance" not in state:
            state["daily_start_balance"] = state.pop("daily_start_nav")
        if "request_count_baseline" in state and "request_count_today" not in state:
            # Old schema — discard the baseline; reset today's count
            state["request_count_today"] = 0
            del state["request_count_baseline"]
        for k, v in defaults.items():
            state.setdefault(k, v)
        # Validate Decimal-typed fields
        state["challenge_start_balance"] = _validated_decimal_str(
            state["challenge_start_balance"], "challenge_start_balance"
        )
        state["daily_start_balance"] = _validated_decimal_str(
            state["daily_start_balance"], "daily_start_balance"
        )
        return state

    def _save_state(self):
        _atomic_write_json(self.state_path, self._state)

    # ---- refresh / day rollover ----

    def _now_monotonic(self):
        return time.monotonic()

    def _cache_stale(self):
        if self._last_refresh_monotonic is None:
            return True
        return self._now_monotonic() - self._last_refresh_monotonic >= self.refresh_ttl_seconds

    def _count_entries_today_from_oanda(self):
        """Count today's ORDER_FILL transactions that opened a trade.

        Filters by transaction time >= today's midnight in FTMO's timezone.
        Returns None if the query fails (caller should keep the existing count).
        """
        midnight_utc = _ftmo_midnight_utc_iso()
        try:
            data = self.api.get_transactions(since_id="0")
        except Exception:
            return None
        count = 0
        for tx in data.get("transactions", []):
            if tx.get("type") != "ORDER_FILL":
                continue
            # Only count entries that opened a new trade — excludes pure closes
            if not tx.get("tradesOpened"):
                continue
            if tx.get("time", "") < midnight_utc:
                continue
            count += 1
        return count

    def refresh(self):
        """Force-refresh account state from OANDA and handle daily rollover."""
        with self._lock:
            try:
                summary = self.api.get_account_summary()
            except Exception as e:
                self._refresh_error = e
                # If we've never had a successful refresh, can't operate at all
                if self._last_refresh_monotonic is None:
                    raise
                return
            self._refresh_error = None

            acct = summary["account"]
            self._balance = Decimal(acct["balance"])
            self._nav = Decimal(acct["NAV"])
            self._open_position_count = int(acct.get("openPositionCount", 0))
            last_tx_id = str(acct.get("lastTransactionID", "0"))
            self._last_refresh_monotonic = self._now_monotonic()

            today = _ftmo_today()

            # First-time initialization of challenge_start_balance
            if self._state["challenge_start_balance"] is None:
                self._state["challenge_start_balance"] = str(self._balance)

            # Daily rollover: anchor to BALANCE (not NAV). Per FTMO's MDL
            # rule, the daily loss = balance_at_00:00_CEST minus current
            # equity. NAV-anchored is less conservative than FTMO's actual
            # calc — a position carrying unrealized loss at midnight would
            # be ignored, allowing the real FTMO limit to be tripped before
            # our local guard fires.
            if self._state["daily_start_date"] != today:
                self._state["daily_start_balance"] = str(self._balance)
                self._state["daily_start_date"] = today

            # Request counter: persist a running total in state so it survives
            # restarts (api.request_count resets to 0 on each process start).
            api_count = self.api.request_count
            last_seen = self._state.get("api_request_count_last_seen", 0)
            if api_count >= last_seen:
                delta = api_count - last_seen
            else:
                # API restarted — treat current api_count as the new increment
                delta = api_count
            if self._state["request_baseline_date"] != today:
                self._state["request_count_today"] = delta
                self._state["request_baseline_date"] = today
            else:
                self._state["request_count_today"] = (
                    int(self._state["request_count_today"]) + delta
                )
            self._state["api_request_count_last_seen"] = api_count

            # Position-entry counter: reconcile from OANDA transactions on:
            # (1) Daily rollover — fresh day, count today's entries from scratch
            # (2) Startup (first refresh of this process) — catches entries
            #     that the local counter missed due to a crash between fill
            #     and record_position_entry()
            is_new_day = self._state["position_entries_date"] != today
            needs_reconcile = is_new_day or not self._reconciled_in_this_process

            if is_new_day:
                self._state["position_entries_today"] = 0
                self._state["position_entries_date"] = today

            if needs_reconcile:
                reconciled = self._count_entries_today_from_oanda()
                if reconciled is not None:
                    self._state["position_entries_today"] = reconciled
                self._reconciled_in_this_process = True

            self._save_state()

    def _ensure_fresh(self):
        if self._cache_stale():
            self.refresh()

    def invalidate_cache(self):
        """Force the next call to refresh, regardless of TTL."""
        with self._lock:
            self._last_refresh_monotonic = None

    # ---- metrics ----

    def daily_pl(self):
        """Signed daily P/L: current equity (NAV) minus today's balance anchor.

        Anchor = balance at 00:00 CE(S)T (FTMO's MDL reference). Current =
        NAV so unrealized P/L is included, matching FTMO's calculation.
        """
        with self._lock:
            self._ensure_fresh()
            start = Decimal(self._state["daily_start_balance"])
            return self._nav - start

    def daily_loss_pct(self):
        """Daily loss as a positive percent of daily start balance, or 0 if in profit."""
        with self._lock:
            self._ensure_fresh()
            start = Decimal(self._state["daily_start_balance"])
            if start <= 0:
                return Decimal("0")
            loss = start - self._nav
            if loss <= 0:
                return Decimal("0")
            return (loss / start) * Decimal("100")

    def total_drawdown_pct(self):
        """Drawdown from challenge start balance, as positive percent. 0 if above start."""
        with self._lock:
            self._ensure_fresh()
            start = Decimal(self._state["challenge_start_balance"])
            if start <= 0:
                return Decimal("0")
            loss = start - self._nav
            if loss <= 0:
                return Decimal("0")
            return (loss / start) * Decimal("100")

    def daily_request_count(self):
        with self._lock:
            self._ensure_fresh()
            return int(self._state["request_count_today"])

    def daily_position_entries(self):
        with self._lock:
            self._ensure_fresh()
            return int(self._state["position_entries_today"])

    def open_position_count(self):
        with self._lock:
            self._ensure_fresh()
            return self._open_position_count

    # ---- enforcement ----

    def can_open_position(self):
        """Return (allowed: bool, reason: str). Reason is empty when allowed.

        Fails closed: if the most recent refresh attempt failed, blocks new
        positions until a successful refresh restores a known-good state.
        """
        with self._lock:
            self._ensure_fresh()

            if self._refresh_error is not None:
                return False, f"Cannot verify account state: {self._refresh_error}"

            loss_pct = self.daily_loss_pct()
            if loss_pct >= self.daily_loss_buffer_pct:
                return False, (
                    f"Daily loss {loss_pct:.2f}% exceeds buffer "
                    f"{self.daily_loss_buffer_pct}%"
                )
            drawdown_pct = self.total_drawdown_pct()
            if drawdown_pct >= self.total_drawdown_buffer_pct:
                return False, (
                    f"Total drawdown {drawdown_pct:.2f}% exceeds buffer "
                    f"{self.total_drawdown_buffer_pct}%"
                )
            req_count = self.daily_request_count()
            if req_count >= self.max_requests_per_day:
                return False, (
                    f"Daily API requests {req_count} exceeds limit "
                    f"{self.max_requests_per_day}"
                )
            entries = self.daily_position_entries()
            if entries >= self.max_position_entries_per_day:
                return False, (
                    f"Daily position entries {entries} exceeds limit "
                    f"{self.max_position_entries_per_day}"
                )
            open_count = self.open_position_count()
            if open_count >= self.max_simultaneous_positions:
                return False, (
                    f"Open positions {open_count} exceeds limit "
                    f"{self.max_simultaneous_positions}"
                )
            return True, ""

    def warnings(self):
        """Return a list of active warnings (not blocking, but operator should know)."""
        with self._lock:
            self._ensure_fresh()
            out = []
            req = self.daily_request_count()
            if req >= REQUEST_WARNING_THRESHOLD:
                out.append(
                    f"Daily API requests {req} approaching limit "
                    f"{self.max_requests_per_day}"
                )
            return out

    def record_position_entry(self):
        """Increment the daily position-entry counter. Strategy code should call
        this after a successful order fill so the guard sees the entry.

        Also invalidates the account-state cache so the next can_open_position()
        call sees the up-to-date NAV (after the order's impact).
        """
        with self._lock:
            self._ensure_fresh()
            self._state["position_entries_today"] += 1
            self._save_state()
            # Invalidate cache so next check sees fresh NAV (filled order moved
            # the account) — prevents the 10s TTL from masking rapid drawdown.
            self._last_refresh_monotonic = None

    # ---- display ----

    def summary(self):
        """Return a dict of all metrics for display/logging."""
        with self._lock:
            self._ensure_fresh()
            return {
                "challenge_type": self.challenge_type,
                "challenge_start_balance": self._state["challenge_start_balance"],
                "daily_start_balance": self._state["daily_start_balance"],
                "current_nav": str(self._nav) if self._nav is not None else None,
                "daily_pl": str(self.daily_pl()) if self._nav is not None else None,
                "daily_loss_pct": f"{self.daily_loss_pct():.2f}",
                "total_drawdown_pct": f"{self.total_drawdown_pct():.2f}",
                "daily_requests": self.daily_request_count(),
                "request_warning_threshold": REQUEST_WARNING_THRESHOLD,
                "max_requests_per_day": self.max_requests_per_day,
                "daily_entries": self.daily_position_entries(),
                "max_entries_per_day": self.max_position_entries_per_day,
                "open_positions": self.open_position_count(),
                "max_simultaneous": self.max_simultaneous_positions,
                "refresh_error": str(self._refresh_error) if self._refresh_error else None,
                "warnings": self.warnings(),
            }
