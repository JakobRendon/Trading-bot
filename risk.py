"""
Risk management primitives: pip sizing, position sizing, and validation.

Pip sizes are hardcoded for the planned pair set. For broader instrument support,
replace with the OANDA instrument metadata cache (see Bundle C in
Trading_Bot_Plan.md).
"""

import time
import uuid
from decimal import Decimal


# Distance from fill where SL/TP is placed: 0.0001 for most pairs (1 pip),
# 0.01 for JPY-quoted pairs (1 pip). Display precision is one more decimal
# place — OANDA accepts the 5th-dp "pipette" for non-JPY pairs.
_PIP_SIZE_DEFAULT = Decimal("0.0001")
_PIP_SIZE_JPY = Decimal("0.01")
_DISPLAY_PRECISION_DEFAULT = 5
_DISPLAY_PRECISION_JPY = 3


def _is_jpy_quote(instrument):
    """JPY-quoted pairs use 3dp prices and 0.01 pip size."""
    return instrument.endswith("_JPY")


def pip_size(instrument):
    return _PIP_SIZE_JPY if _is_jpy_quote(instrument) else _PIP_SIZE_DEFAULT


def display_precision(instrument):
    return _DISPLAY_PRECISION_JPY if _is_jpy_quote(instrument) else _DISPLAY_PRECISION_DEFAULT


def pip_distance(instrument, pips):
    """Convert a pip count to an OANDA-format price-distance string."""
    distance = Decimal(pips) * pip_size(instrument)
    precision = display_precision(instrument)
    return f"{distance:.{precision}f}"


def position_size(
    balance,
    risk_pct,
    stop_loss_pips,
    instrument,
    account_currency="USD",
    quote_to_account_rate=None,
):
    """Compute position size in units.

    Formula: units = (balance * risk%) / (stop_loss_pips * pip_size * quote_to_account_rate)

    The instrument's quote currency (e.g., USD in EUR_USD, JPY in USD_JPY)
    determines whether a conversion factor is needed:
    - If quote currency == account_currency: factor is 1 automatically.
    - If they differ: quote_to_account_rate is REQUIRED (no silent default to 1).
      Pass the current rate quoting "1 quote_currency in account_currency".
      Example: USD_JPY on USD account → pass ~0.0067 (i.e., 1/USDJPY rate).

    Raises ValueError if the rate is needed but not provided — silent default-to-1
    would mis-size by orders of magnitude on cross-currency pairs.

    Returns an int — OANDA accepts fractional units on some pairs, but
    integer rounding keeps sizing predictable and avoids precision footguns.
    """
    if stop_loss_pips <= 0:
        raise ValueError("stop_loss_pips must be positive")
    if risk_pct <= 0:
        raise ValueError("risk_pct must be positive")

    quote_currency = instrument.split("_")[1] if "_" in instrument else None
    if quote_currency == account_currency:
        rate = Decimal("1")
    elif quote_to_account_rate is None:
        raise ValueError(
            f"position_size for {instrument} on a {account_currency} account "
            f"requires quote_to_account_rate (rate from {quote_currency} to "
            f"{account_currency}). Pass an explicit value to avoid silent mis-sizing."
        )
    else:
        rate = Decimal(str(quote_to_account_rate))

    risk_amount = Decimal(str(balance)) * Decimal(str(risk_pct)) / Decimal("100")
    pip_value_per_unit = pip_size(instrument) * rate
    units = risk_amount / (Decimal(stop_loss_pips) * pip_value_per_unit)
    return int(units)


def validate_risk_reward(stop_loss_pips, take_profit_pips, min_ratio=Decimal("1.5")):
    """Raise ValueError if TP/SL ratio is below min_ratio.

    The plan mandates 1:1.5 minimum reward-to-risk for any trade.
    """
    if stop_loss_pips <= 0 or take_profit_pips <= 0:
        raise ValueError("stop_loss_pips and take_profit_pips must be positive")
    ratio = Decimal(take_profit_pips) / Decimal(stop_loss_pips)
    if ratio < Decimal(str(min_ratio)):
        raise ValueError(
            f"Risk-reward ratio {ratio:.2f} below minimum {min_ratio} "
            f"(SL: {stop_loss_pips} pips, TP: {take_profit_pips} pips)"
        )


def generate_client_id(prefix="bot"):
    """Generate a unique client ID for order idempotency.

    Format: <prefix>-<unix_seconds>-<8hex>. Allows lookup via OANDA's
    `@client_id` order specifier syntax for safe POST-retry behavior.
    """
    return f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
