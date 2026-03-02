# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OANDA v20 REST API trading bot targeting FTMO prop trading. Currently bare-bones: pulls EUR/USD data and executes trades on an OANDA practice account. No trading strategy logic yet.

## Running

```bash
pip install -r requirements.txt
python main.py          # interactive menu (requires terminal input)
```

For non-interactive testing (e.g., from Claude Code):
```python
import config
from oanda_api import OandaAPI
api = OandaAPI(config.API_TOKEN, config.ACCOUNT_ID, config.BASE_URL)
api.get_account_summary()
```

## Architecture

- **config.py** — Loads `.env` credentials, exposes `API_TOKEN`, `ACCOUNT_ID`, `BASE_URL`. Environment (`practice`/`live`) controls which OANDA server is used.
- **oanda_api.py** — `OandaAPI` class wrapping direct HTTP calls (`requests`) to the OANDA v20 REST API. All endpoints go through `_get`, `_post`, `_put` helpers. No external API wrapper libraries.
- **main.py** — Interactive CLI menu for manual testing. Uses `input()` so cannot be driven non-interactively.

## OANDA API Details

- Base URLs: `https://api-fxpractice.oanda.com` (practice), `https://api-fxtrade.oanda.com` (live)
- All endpoints are under `/v3/accounts/{accountID}/...`
- Auth: Bearer token in `Authorization` header
- Instruments use underscore format: `EUR_USD`, not `EUR/USD`
- Order units: positive = buy (long), negative = sell (short), passed as string

## FTMO Constraints (design around these)

- Max 2,000 API requests/day
- Max 200 simultaneous orders
- Max 2,000 position entries/day
- 5% max daily loss, 10% max total drawdown
- Automated trading is allowed; HFT arbitrage is not
