from decimal import Decimal
import pytest
from risk import (
    pip_size,
    display_precision,
    pip_distance,
    position_size,
    validate_risk_reward,
    generate_client_id,
)


class TestPipSize:
    def test_eur_usd_pip_size(self):
        assert pip_size("EUR_USD") == Decimal("0.0001")

    def test_gbp_usd_pip_size(self):
        assert pip_size("GBP_USD") == Decimal("0.0001")

    def test_usd_jpy_pip_size(self):
        assert pip_size("USD_JPY") == Decimal("0.01")

    def test_eur_jpy_pip_size(self):
        assert pip_size("EUR_JPY") == Decimal("0.01")

    def test_eur_gbp_pip_size(self):
        assert pip_size("EUR_GBP") == Decimal("0.0001")


class TestDisplayPrecision:
    def test_eur_usd_precision(self):
        assert display_precision("EUR_USD") == 5

    def test_usd_jpy_precision(self):
        assert display_precision("USD_JPY") == 3


class TestPipDistance:
    def test_eur_usd_30_pips(self):
        # 30 * 0.0001 = 0.0030, formatted to 5dp
        assert pip_distance("EUR_USD", 30) == "0.00300"

    def test_usd_jpy_30_pips(self):
        # 30 * 0.01 = 0.30, formatted to 3dp
        assert pip_distance("USD_JPY", 30) == "0.300"

    def test_eur_usd_100_pips(self):
        assert pip_distance("EUR_USD", 100) == "0.01000"

    def test_distance_is_string(self):
        # OANDA expects price values as strings
        assert isinstance(pip_distance("EUR_USD", 10), str)


class TestPositionSize:
    def test_basic_eur_usd_sizing(self):
        # $10,000 balance, 1% risk = $100 risk
        # 30 pip SL on EUR/USD = 30 * 0.0001 = $0.003 per unit
        # Units = $100 / $0.003 = 33,333
        assert position_size(10000, 1, 30, "EUR_USD") == 33333

    def test_smaller_balance(self):
        # $25,000 balance (FTMO challenge), 0.5% risk = $125 risk
        # 50 pip SL on EUR/USD = 50 * 0.0001 = $0.005 per unit
        # Units = $125 / $0.005 = 25,000
        assert position_size(25000, 0.5, 50, "EUR_USD") == 25000

    def test_ftmo_100k_account_1pct_risk(self):
        # $100k, 1%, 20 pip SL = $1000 risk / $0.002 per unit = 500,000 units
        assert position_size(100000, 1, 20, "EUR_USD") == 500000

    def test_zero_stop_loss_raises(self):
        with pytest.raises(ValueError, match="stop_loss_pips"):
            position_size(10000, 1, 0, "EUR_USD")

    def test_negative_stop_loss_raises(self):
        with pytest.raises(ValueError, match="stop_loss_pips"):
            position_size(10000, 1, -10, "EUR_USD")

    def test_zero_risk_pct_raises(self):
        with pytest.raises(ValueError, match="risk_pct"):
            position_size(10000, 0, 20, "EUR_USD")

    def test_jpy_pair_sizing_with_conversion(self):
        # For USD_JPY on USD account, pip value is in JPY.
        # 1 JPY ~= 0.0067 USD (rate of ~150 JPY/USD)
        # So quote_to_account = 1/150 = 0.00667
        # $10,000 balance, 1% risk = $100
        # Per-unit risk = 0.01 (pip) * 0.00667 = 0.0000667 USD
        # Units = 100 / (30 * 0.0000667) ≈ 49975
        result = position_size(10000, 1, 30, "USD_JPY", quote_to_account_rate=Decimal("0.00667"))
        assert 49000 < result < 51000

    def test_returns_int(self):
        result = position_size(10000, 1, 30, "EUR_USD")
        assert isinstance(result, int)


class TestValidateRiskReward:
    def test_15_ratio_passes(self):
        validate_risk_reward(20, 30)  # 1:1.5

    def test_2_ratio_passes(self):
        validate_risk_reward(20, 40)  # 1:2

    def test_1_ratio_raises(self):
        with pytest.raises(ValueError, match="Risk-reward"):
            validate_risk_reward(20, 20)  # 1:1

    def test_below_15_raises(self):
        with pytest.raises(ValueError, match="Risk-reward"):
            validate_risk_reward(20, 25)  # 1:1.25

    def test_custom_min_ratio(self):
        # 1:2 minimum
        validate_risk_reward(20, 40, min_ratio=2)
        with pytest.raises(ValueError):
            validate_risk_reward(20, 30, min_ratio=2)

    def test_zero_sl_raises(self):
        with pytest.raises(ValueError):
            validate_risk_reward(0, 30)

    def test_zero_tp_raises(self):
        with pytest.raises(ValueError):
            validate_risk_reward(20, 0)


class TestGenerateClientId:
    def test_default_prefix(self):
        cid = generate_client_id()
        assert cid.startswith("bot-")

    def test_custom_prefix(self):
        cid = generate_client_id("strategy-A")
        assert cid.startswith("strategy-A-")

    def test_ids_are_unique(self):
        ids = {generate_client_id() for _ in range(100)}
        assert len(ids) == 100  # No collisions

    def test_contains_timestamp_segment(self):
        cid = generate_client_id()
        parts = cid.split("-")
        # bot-<unix_secs>-<hex8>
        assert len(parts) == 3
        assert parts[1].isdigit()
        assert len(parts[2]) == 8
