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


def position_size(balance, risk_pct, stop_loss_pips, instrument, quote_to_account_rate=Decimal("1")):
    """Compute position size in units.

    Formula: units = (balance * risk%) / (stop_loss_pips * pip_size * quote_to_account_rate)

    Assumes account currency == quote currency by default (quote_to_account_rate=1).
    Correct for pairs like EUR_USD or GBP_USD on a USD-denominated account.
    For pairs where the quote currency differs from the account currency
    (e.g., USD_JPY on a USD account: quote is JPY), pass the current rate
    quoting the conversion from quote to account currency.

    Returns an int — OANDA accepts fractional units on some pairs, but
    integer rounding keeps sizing predictable and avoids precision footguns.
    """
    if stop_loss_pips <= 0:
        raise ValueError("stop_loss_pips must be positive")
    if risk_pct <= 0:
        raise ValueError("risk_pct must be positive")

    risk_amount = Decimal(str(balance)) * Decimal(str(risk_pct)) / Decimal("100")
    pip_value_per_unit = pip_size(instrument) * Decimal(str(quote_to_account_rate))
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
