import os
from dotenv import load_dotenv

load_dotenv()

API_TOKEN = os.getenv("OANDA_API_TOKEN")
ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID")

_environment = os.getenv("OANDA_ENVIRONMENT", "practice")

if _environment == "live":
    BASE_URL = "https://api-fxtrade.oanda.com"
else:
    BASE_URL = "https://api-fxpractice.oanda.com"


def _parse_list(env_var, default):
    raw = os.getenv(env_var, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


INSTRUMENTS = _parse_list("OANDA_INSTRUMENTS", "EUR_USD")
GRANULARITIES = _parse_list("OANDA_GRANULARITIES", "M5,M15,H1,H4")

# FTMO risk thresholds — see Trading_Bot_Plan.md for sourcing.
# Buffers are deliberately below FTMO's hard limits to leave headroom.
RISK_STATE_PATH = os.getenv("RISK_STATE_PATH", "risk_state.json")
CHALLENGE_START_BALANCE = os.getenv("CHALLENGE_START_BALANCE") or None

# Challenge type: "2-step" (5% daily, 10% static drawdown) or "1-step"
# (3% daily, 10% trailing drawdown). FTMO introduced the 1-Step in Feb 2026.
CHALLENGE_TYPE = os.getenv("CHALLENGE_TYPE", "2-step").lower()
if CHALLENGE_TYPE not in ("1-step", "2-step"):
    raise ValueError(
        f"CHALLENGE_TYPE must be '1-step' or '2-step', got: {CHALLENGE_TYPE!r}"
    )

# Default buffers sit 1% under each challenge's hard limit. Override via
# env var for tighter / looser headroom.
_DEFAULT_DAILY_BUFFER = {"1-step": 2.0, "2-step": 4.0}[CHALLENGE_TYPE]
DAILY_LOSS_BUFFER_PCT = float(os.getenv("DAILY_LOSS_BUFFER_PCT", str(_DEFAULT_DAILY_BUFFER)))
TOTAL_DRAWDOWN_BUFFER_PCT = float(os.getenv("TOTAL_DRAWDOWN_BUFFER_PCT", "9"))
MAX_REQUESTS_PER_DAY = int(os.getenv("MAX_REQUESTS_PER_DAY", "1900"))
MAX_POSITION_ENTRIES_PER_DAY = int(os.getenv("MAX_POSITION_ENTRIES_PER_DAY", "1900"))
MAX_SIMULTANEOUS_POSITIONS = int(os.getenv("MAX_SIMULTANEOUS_POSITIONS", "180"))
