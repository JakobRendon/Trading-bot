# Trading Bot Development Plan

Target: FTMO prop trading via OANDA v20 API. Starting with $25k challenge, scaling to $100k-$200k.

---

## Phase 1: Core API Client ✅ DONE
- OANDA v20 REST API connection (direct HTTP, no wrapper library)
- Account summary, candle data, pricing, market orders, position management
- Practice environment with configurable live switch

---

## Phase 2: Price Streaming & Candle Engine
Move from polling REST endpoints to OANDA's streaming API for real-time data.

**Streaming connection:**
- Connect to `https://stream-fxpractice.oanda.com/v3/accounts/{id}/pricing/stream`
- Uses chunked transfer encoding (not SSE) — consume with `requests` + `stream=True` + `iter_lines()`
- Heartbeat every 5 seconds to keep connection alive
- Max 4 price updates/second per instrument, max 20 streams per IP
- Streaming does NOT count against FTMO's 2,000 request/day limit — only order execution calls do

**Candle aggregation:**
- Build candles locally from tick data (no extra API calls)
- Support multiple granularities simultaneously (M5, M15, H1, H4)
- Detect candle close events to trigger strategy evaluation
- Handle reconnection gracefully — rebuild partial candle state on reconnect

**Configurable instruments:**
- EUR/USD for initial development and testing of the streaming/candle engine
- Switch to strategy-appropriate pairs once strategies are implemented (GBP/USD for London Breakout, EUR/GBP for Mean Reversion)
- Design for multi-pair from day one — instrument list loaded from config

---

## Phase 3: Risk Management
Two layers: per-trade controls and FTMO account-level limits.

**Per-trade risk:**
- Stop-loss and take-profit on every order (no naked positions)
- Position sizing: risk a fixed % of account per trade (0.5–1%)
- Formula: `position_size = (balance × risk%) / (stop_loss_pips × pip_value)`
- ATR-based dynamic stop-loss — wider stops in volatile markets = smaller position
- Minimum 1:1.5 risk-reward ratio enforced before entry

**FTMO compliance layer:**
- Track daily P/L in real-time — auto-stop trading if approaching 4% daily loss (buffer before 5% hard limit)
- Track total drawdown — auto-stop if approaching 9% (buffer before 10% hard limit)
- Count API requests per day — warn at 1,500, hard stop at 1,900
- Count position entries per day — hard stop at 1,900 (limit is 2,000)
- Count simultaneous open orders — hard stop at 180 (limit is 200)
- Daily P/L resets at midnight CE(S)T
- All limits configurable per account size

---

## Phase 4: Logging & Notifications

**File logging:**
- Every trade: entry time, instrument, direction, units, price, SL, TP, P/L on close
- Daily summary: trades taken, win/loss count, daily P/L, drawdown status
- Risk events: when limits are approached or hit
- Errors: API failures, reconnections, unexpected responses

**Email alerts:**
- Trade opened / closed
- Daily loss limit approaching (e.g., at 3% and 4%)
- Total drawdown limit approaching
- Bot stopped (error or limit hit)
- Daily summary email

---

## Phase 5: Strategy Engine
Pluggable framework — strategies are interchangeable modules with a common interface.

**Framework design:**
- Each strategy receives candle data and returns a signal (buy, sell, or no action)
- Strategy does NOT handle execution, risk, or position sizing — those are separate layers
- Strategies are stateless per evaluation (receive a window of candles, return a signal)
- Config selects which strategy is active

**First strategy — London Breakout:**
Best documented success rate for FTMO challenges. Based on research:
- Best pairs: GBP/USD (top pick), EUR/USD, USD/JPY
- Identify the Asian session range (00:00–07:00 GMT)
- At London open (08:00–10:00 GMT), enter on breakout of that range
- Confirmation: M15 candle closes beyond range high/low
- Stop-loss: opposite side of the range
- Take-profit: 1.5× to 2× the range width
- One trade per day max
- Typical: ~50-55% win rate, profit factor >1.5

**Second strategy — Mean Reversion + Divergence:**
Good for ranging/consolidating markets:
- Best pairs: EUR/GBP (top pick — rarely trends), USD/JPY
- Entry: price closes outside Bollinger Bands (20, 2 SD) + RSI < 35 or > 65 + MACD divergence confirmation
- Stop-loss: recent swing high/low
- Take-profit: middle Bollinger Band (20 SMA) or 2:1 RRR
- Higher timeframe trend filter (H4/Daily EMA 50/200) to avoid counter-trend entries
- 1–3 trades per day

**Indicators to implement:**
- EMA (50, 200) — trend identification
- Bollinger Bands (20, 2 SD) — overbought/oversold
- RSI (14) — momentum confirmation
- MACD (5, 34, 5) — divergence detection
- ATR (14) — volatility / dynamic stop-loss sizing

---

## Phase 6: Backtesting
Test strategies against historical data before risking real money.

**Historical data:**
- Fetch candles from OANDA REST API (supports 5-second to monthly granularity)
- Store locally to avoid repeated API calls
- Minimum 1 year of data per instrument/granularity

**Backtesting engine:**
- Feed historical candles through strategy + risk management layers
- Simulate order fills at candle close prices
- Track: total P/L, win rate, max drawdown, profit factor, Sharpe ratio
- Simulate FTMO rules (daily loss, total drawdown) to see if the strategy would pass
- Output: summary stats + trade-by-trade log

**Walk-forward validation (required — do not skip):**
- Split data into rolling windows: train on 2 years, test on next 3 months unseen, roll forward and repeat
- Only trust out-of-sample results — in-sample performance is meaningless
- Do not tweak window sizes after seeing results (meta-overfitting)

**Overfitting safeguards:**
- Sensitivity test: vary strategy parameters ±10% — if results collapse, the edge is fragile and not tradeable
- Minimum out-of-sample Sharpe of 1.0 before going live (expect 30–50% degradation from backtest to live)
- Same parameters must work across multiple pairs — per-pair tuning is a red flag

**Forward testing:**
- Paper trade mode: run live against streaming data but don't place real orders
- Compare paper results to backtest results before going live
- Minimum 1–2 weeks forward testing recommended

---

## Phase 7: Production Hardening

**Reconnection & error recovery:**
- Auto-reconnect streaming on disconnect with exponential backoff
- Rebuild candle state from REST API after reconnect
- Persist bot state (open positions, daily P/L) to disk so restarts don't lose tracking

**Session awareness:**
- Each strategy defines its own session windows, not a single global setting:
  - London Breakout: monitor Asian range 00:00–07:00 GMT (data collection, no trades), trade at London open 08:00–10:00 GMT
  - Mean Reversion: active during London 08:00–17:00 GMT and/or New York 12:00–21:00 GMT (highest liquidity for reversions)
- News filter: reduce position size or skip trades around major economic releases (NFP, CPI, FOMC)
- On funded FTMO accounts: 2-minute no-trade window before/after major news

**Execution:**
- Run as a long-lived process (not cron)
- Graceful shutdown: close no positions, just stop entering new trades
- Startup check: reconcile bot state with actual OANDA account state

---

## Phase 8: Dashboard & Scaling (Future)

**Web dashboard:**
- Real-time view of open positions, P/L, drawdown status
- Trade history and performance charts
- Start/stop bot controls

**Multi-account scaling:**
- Support multiple OANDA accounts (different FTMO challenges)
- Per-account config (size, risk %, strategy)
- Scale from $25k → $100k → $200k with same bot, different parameters

---

## Key Numbers to Remember

| Rule | Limit |
|------|-------|
| FTMO daily loss | 5% (use 4% buffer) |
| FTMO total drawdown | 10% (use 9% buffer) |
| FTMO profit target (challenge) | 10% |
| FTMO profit target (verification) | 5% |
| FTMO min trading days | 4 |
| OANDA API requests/day (FTMO) | 2,000 |
| OANDA simultaneous orders (FTMO) | 200 |
| OANDA position entries/day (FTMO) | 2,000 |
| Risk per trade | 0.5–1% of balance |
| Risk-reward ratio minimum | 1:1.5 |
| Trades per day target | 1–4 |
| OANDA streaming rate | 4 prices/sec/instrument |
| OANDA REST rate limit | 120 req/sec per IP |

---

## Notes

**Best pairs per strategy:**
- London Breakout: GBP/USD (top pick — Sterling dominates London session), EUR/USD, USD/JPY
- Mean Reversion: EUR/GBP (rarely trends, clean reversions), USD/JPY
- OANDA offers 68+ forex pairs. Pull the full list for your account via the `/v3/accounts/{id}/instruments` endpoint.
- OANDA EUR/USD spread: ~1.06 pips, GBP/USD: ~1.86 pips. Tighter spreads = lower cost per trade.
- FTMO's most successful traders focus on 1–4 instruments max. More than that becomes counterproductive.

**Overfitting warning for backtesting:**
- The more pairs/parameters you optimize, the higher the risk of finding "lucky" patterns that fail live.
- One documented case: backtest Sharpe 1.59 collapsed to -0.18 out-of-sample.
- Expect 30–50% performance degradation from backtest to live trading. If a strategy only shows marginal edge in backtests, don't trade it.
- Strategies that work across many pairs with the same parameters are far more trustworthy than ones needing unique tuning per pair.

**Walk-forward analysis (use this for all backtesting):**
- Train on 2 years of data, test on the next 3 months (unseen), roll forward and repeat.
- Only trust results from unseen data, never the training period.
- Don't tweak walk-forward window sizes to get better results — that's meta-overfitting.

**Future separate project — Pair Scanner:**
Systematically evaluate trading strategies across all 68+ OANDA currency pairs to find which pairs each strategy performs best on. This should be a standalone tool, not part of the live bot, because:
- It's computationally heavy (every strategy × every pair × walk-forward windows)
- It needs its own validation pipeline (walk-forward + out-of-sample + Monte Carlo simulation)
- Results feed back into the bot's config (which pairs to trade) but the scanning process is independent
- Risk of overfitting is high — needs rigorous statistical controls (Deflated Sharpe Ratio, Bonferroni correction for multiple testing)
- Professional quant firms run this as a separate research process, not embedded in production trading systems
