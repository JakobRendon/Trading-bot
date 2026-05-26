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
DAILY_LOSS_BUFFER_PCT = float(os.getenv("DAILY_LOSS_BUFFER_PCT", "4"))
TOTAL_DRAWDOWN_BUFFER_PCT = float(os.getenv("TOTAL_DRAWDOWN_BUFFER_PCT", "9"))
MAX_REQUESTS_PER_DAY = int(os.getenv("MAX_REQUESTS_PER_DAY", "1900"))
MAX_POSITION_ENTRIES_PER_DAY = int(os.getenv("MAX_POSITION_ENTRIES_PER_DAY", "1900"))
MAX_SIMULTANEOUS_POSITIONS = int(os.getenv("MAX_SIMULTANEOUS_POSITIONS", "180"))
