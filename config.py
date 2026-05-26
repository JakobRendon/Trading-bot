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
