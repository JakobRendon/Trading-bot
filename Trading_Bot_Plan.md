# Trading Bot Development Plan

Target: FTMO prop trading via OANDA v20 API. Starting with $25k challenge, scaling to $100k-$200k.

---

## 2026-05-27 Review Outcome

A multi-agent pre-Phase-4 review surfaced 7 critical bugs, FTMO compliance gaps, and backtest realism issues. Resolved in a single bundle pass on 2026-05-27.

**Landed:**
- All Bundle A bugs (KeyError crash, cross-currency position sizing, eager `_last_trade_date`, stream/aggregator exception isolation, position-stacking guard, backtester explicit-units validation)
- B1: `CHALLENGE_TYPE` config drives daily-loss buffer (2-step: 4%, 1-step: 2%)
- B3: Daily anchor reverted to **balance** (FTMO MDL rule) — was incorrectly anchored on NAV
- B6: Backtester now flags FTMO daily-loss / total-drawdown breaches per challenge type
- C1+C2: Backtester models `spread_pips` (bid/ask shift) and SL gap-through fills
- D4: Backtester calls `strategy.on_candle_close()` every candle (parity with live runner post-A5)
- D5: Walk-forward windows now emit a filtered equity curve so max-drawdown reflects the window

**Headline finding:** Re-running the MeanReversion EUR/GBP backtest with realistic spread (2 pips) and gap-through fills turned the previous "+$19.6k" headline into **-$2.9k with 19.3% max drawdown** (would fail FTMO daily-loss + total-drawdown). Walk-forward at spread=2 was profitable in only 1 of 4 windows. **MeanReversion is not a deployable strategy at current parameters.**

**Deferred** (each is parked behind a specific trigger; pick it up when the trigger fires):

- **B2 — Trailing drawdown for 1-Step Challenge**
  - *Trigger:* before running the bot against a 1-Step FTMO account.
  - *What:* 1-Step's total-drawdown floor is `max(challenge_start_balance, peak_EOD_equity) − 10%`, ratcheting up with profit. Today both `risk_guard.total_drawdown_pct()` and `Backtester._FTMO_LIMITS["1-step"]` compute drawdown vs the static starting balance (matches 2-Step only).
  - *Where:* `risk_guard.py:total_drawdown_pct` + persisted state needs a `peak_eod_equity` field; `backtest.py:run()` FTMO check would need the same trailing peak.
  - *Sources:* FTMO×OANDA MDL FAQ.

- **B4 — Minimum trading days (≥4 per phase, 2-Step)**
  - *Trigger:* as the bot approaches a profit target near phase-end, or before live deployment on any challenge.
  - *What:* Track distinct trading days (UTC or Prague — match the FTMO timezone the rest of the guard uses). If the bot hits the profit target on day 1 and stops, FTMO auto-fails the phase. Need to either keep emitting at least one tiny trade per remaining day, or pause withdrawing-eligible behavior until day-4 is reached.
  - *Where:* `risk_guard.py` — new persisted state field `trading_days_count` incremented in `record_position_entry`. Surface in `summary()` so `risk_status` shows it. Add a `min_trading_days_remaining()` helper.

- **B5 — News blackout for funded Standard accounts**
  - *Trigger:* before any account converts from Verification to Funded Standard. (2026 rules: no restriction in Challenge / Verification; ±2 min around major news on funded Standard; no restriction on funded Swing.)
  - *What:* Maintain a calendar of high-impact events (manual CSV, or Forex Factory / ECB feed). Block `place_market_order` and pre-emptively close positions within the ±2-min window. SL/TP fills inside the window also count.
  - *Where:* New `news_calendar.py` module; gate added in `oanda_api.place_market_order` and in `strategy_runner._handle_signal`. Likely needs a separate background poller to close before the window opens.

- **B7 — Plan doc 1-Step vs 2-Step parameter split**
  - *Trigger:* when this plan doc gets re-read by a fresh agent / on the next major doc cleanup.
  - *What:* The "FTMO Constraints" section in `CLAUDE.md` and various Phase 3 notes currently cite 2-Step numbers as if they were universal. Split into a table that shows both columns: daily loss limit (5% / 3%), drawdown rule (static / trailing), min trading days (≥4 / not stated), best-day rule (funded-only / challenge+funded).

- **C3 — SL slippage modeling beyond gap-through**
  - *Trigger:* when the backtest result for any deployable strategy is close enough to break-even that 0.5-1 pip per SL hit matters (e.g., a profit factor between 1.0 and 1.2).
  - *What:* On every SL fill (not just gap-through), add a configurable slippage (e.g., `sl_slippage_pips=0.5`) so the fill is N pips beyond the stop. Today only gap-through is modeled; intra-candle slippage from fast moves is invisible.
  - *Where:* `backtest.py:_check_exit` — adjust the "sl" branch fill price by `+sl_slippage_pips * pip_size` (long) / `-` (short). Add `sl_slippage_pips` to `Backtester.__init__` + propagate through `WalkForwardAnalyzer`.

- **D1 — `pip_size` allow-list for non-FX instruments**
  - *Trigger:* before adding XAU/BTC/indices to `OANDA_INSTRUMENTS`. Currently `risk.pip_size` returns 0.0001 for anything non-JPY — gold uses 0.10, BTC uses 1.0, indices vary. Mis-sizing by 100×–100,000×.
  - *What:* Replace the `_is_jpy_quote` heuristic in `risk.py` with an allow-list (`XAU_USD: 0.10`, `XAG_USD: 0.01`, `BTC_USD: 1.0`, indices per OANDA spec). Raise on unknown instruments rather than defaulting silently. Alternative: fetch instrument metadata from OANDA at startup and cache it.

- **D2 — DST handling in LondonBreakout**
  - *Trigger:* if LondonBreakout is revived as a viable strategy. Currently it had 0% profitable walk-forward windows on GBP/USD at default params and is de-prioritized.
  - *What:* `LondonBreakoutStrategy` uses fixed UTC hours (8-10 = London BST = summer). In winter (GMT) London opens at 09:00 UTC, so the strategy fires an hour early for ~6 months. Convert the window to Europe/London zone and derive UTC hours per candle's date.
  - *Where:* `london_breakout.py:on_candle_close` — replace `candle_hour` with `candle_dt.astimezone(ZoneInfo("Europe/London")).hour`.

- **D3 — Mean Reversion signal hysteresis**
  - *Trigger:* if Mean Reversion is revived (it's currently unprofitable at realistic spread). Now that A5 blocks stacking in the runner, the only impact would be on the very first re-entry after a position closes.
  - *What:* After a BB-breach signal, require price to cross back inside the band before re-arming. Today `mean_reversion.py:on_candle_close` emits on every candle where `close < bb.lower and rsi < oversold` — fine while a position is open (A5 blocks), but as soon as that trade closes, the strategy can re-fire immediately.
  - *Where:* `mean_reversion.py` — add `_armed_long` / `_armed_short` flags; flip them off after emission, flip back on when close crosses back through the band.

- **D6 — Non-USD account-currency wiring**
  - *Trigger:* if a FTMO account denominated in EUR / GBP / CZK is funded. The `account_currency` parameter already exists on `StrategyRunner.__init__` and `Backtester.__init__`; only the wiring is missing.
  - *What:* Add `ACCOUNT_CURRENCY` env var to `config.py`; pull it from OANDA's account summary `currency` field on first refresh and warn if it doesn't match. Pass `config.ACCOUNT_CURRENCY` to `StrategyRunner` and `Backtester` instead of relying on the USD default.
  - *Where:* `config.py`, `main.py` (the four places that construct these classes).

**2026 FTMO rule changes (captured during the review):**
- News restriction REMOVED for Challenge/Verification phases — only funded Standard accounts still have ±2-min window
- 1-Step Challenge introduced (Feb 2026): 3% daily loss, 10% trailing drawdown (vs 2-Step's 5% / 10% static)
- FTMO×OANDA: 42 forex + 6 other instruments, $200k max simulated account size

---

## Phase 1: Core API Client ✅ DONE
- OANDA v20 REST API connection (direct HTTP, no wrapper library)
- Account summary, candle data, pricing, market orders, position management, transactions
- Practice environment with configurable live switch
- Test suite (unit, integration, live trade) — 34 tests total

**Deferred API methods (add when consuming phase needs them):**
- Orders with attached SL/TP (needed for Phase 3 risk management)
- Date-range candle fetching (needed for Phase 6 backtesting — current method only does "last N candles")
- List tradeable instruments via `/v3/accounts/{id}/instruments` (needed for Phase 5 multi-pair support)
- Modify/cancel pending orders (needed when we add limit orders)

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

## Phase ordering note (decided 2026-05-26)

Building Phase 5 (Strategy Engine) **before** Phase 4 (Logging & Notifications).

Rationale:
- Strategies define what's worth logging. Building Phase 4 first risks logging the wrong events or missing the right ones.
- Phase 4's email alerts are most valuable for *unattended* trading — and the bot isn't unattended until strategies run autonomously.
- Logging is horizontal infrastructure; the call sites it needs to wrap are in the strategy layer that doesn't exist yet.

Compromise: add minimal stdlib `logging.getLogger(__name__)` calls during Phase 5 development for debugging. Phase 4 then builds the full audit-trail story (file rotation, email alerts, structured format, daily summaries) on top of strategies' actual event surface.

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
| FTMO profit target (2-step challenge) | 10% |
| FTMO profit target (2-step verification) | 5% |
| FTMO profit target (1-step challenge) | 10% (tighter rules: 3% daily loss, 10% trailing drawdown) |
| FTMO min trading days | 4 |
| FTMO challenge time limit | Unlimited (30-day cap removed) |
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

**FTMO + OANDA ownership change (Dec 2025):**
FTMO acquired OANDA Group on December 1, 2025. OANDA's own Prop Trader programme was discontinued March 31, 2026. Key implications:
- The OANDA v20 API is still active and unchanged — our code continues to work
- FTMO traders are migrating to `ftmo.oanda.com` as the unified platform
- Verify that practice account credentials still work before starting development; may need to set up through the new FTMO x OANDA portal
- Execution now routes through FTMO Group internally rather than third-party

**FTMO rule changes (updated May 2026):**
- New 1-Step Challenge option (Feb 2026): 10% target, single phase, but 3% daily loss limit and 10% trailing drawdown
- Challenge time limit removed — unlimited time to hit profit target
- Martingale/grid no longer explicitly banned, but subject to closer scrutiny (we still avoid them)
- Swing accounts now allow full news trading (no 2-minute restriction)
- MetaTrader 5 and TradingView added as official platform options

**Strategy landscape (updated May 2026):**
- London Breakout still effective: May 2025 GBP/USD backtest showed 52.6% win rate, 1.74 profit factor
- New detail: skip London Breakout setups where Asian range is narrower than 80 pips on GBP/USD
- Mean Reversion still viable: MACD + Bollinger Bands showing 78% win rate in recent backtests
- EUR/GBP still range-bound (€1.14–1.16), confirming suitability for mean reversion
- Spreads expected to widen in 2026 — backtests should use realistic current spread data
- Fair Value Gap (FVG) and Smart Money Concepts (SMC) strategies gaining traction — consider as future strategy additions

---

## Known Issues & Future Cleanup

Identified during a multi-agent code review (May 2026). These don't block development but should be addressed before going live or before they bite in later phases.

**Code quality / robustness:**
- **Transaction pagination unhandled** — `get_transactions(since_id="0")` returns the full account history in one shot. OANDA caps responses at ~1000 transactions and uses pagination via the `pages` field for larger histories. Once the practice account has run long enough, history will be silently truncated. Fix: walk pagination until empty.
- **`mid_price` Decimal precision drift on JPY pairs** — `(bid+ask)/2` for 3dp prices like JPY produces an artificial 4th decimal. Doesn't affect candle math itself, but downstream code comparing aggregator prices to broker prices may never see equality. Fix: quantize result to the instrument's display precision (solved by instrument metadata cache below).
- **CandleAggregator skips empty buckets during gaps** — During weekends/halts, intermediate buckets (e.g., M5 candles at 02:05, 02:10, 02:15) are never emitted. Strategies counting "last N candles" will see invisible gaps. Fix: when a tick arrives in a much-later bucket, emit empty/flat candles for all skipped buckets between.
- **`parse_tick_time` fragile if OANDA ever sends offset-format timestamps** — Current code uses `rstrip("Z")` then `.replace(tzinfo=UTC)`. If OANDA ever returns `+00:00` style, the replace silently clobbers any non-UTC offset. Theoretical right now. Fix: use `datetime.fromisoformat()` properly without the strip/replace pattern (Python 3.11+ accepts `Z` directly).
- **`OANDA_ENVIRONMENT` accepts any value as practice** — Case-sensitive, no validation. A typo like `Practice` or `production` silently routes to the practice environment. Fix: validate against `{"practice", "live"}` and raise on unknown.

**Test gaps:**
- **No test for high tick rate / all granularities at once** — All aggregator tests use 1–4 ticks. A stress test with hundreds of ticks across `M1/M5/M15/M30/H1/H4/D` simultaneously would catch precision drift, performance issues, and bucket-tracking bugs.
- **No test for `stop()` interrupting blocking `iter_lines()`** — Current tests use synchronous mocks, so `stop()` always takes effect on the next mocked event. Real-world `stop()` from another thread while the stream is blocked on a quiet socket won't take effect until the next event or the 10s heartbeat timeout. Worth a test using a slow generator mock.
- **No test for malformed partial JSON lines mid-stream** — Current "invalid json" test uses obviously bad input. A more realistic case is a truncated event like `{"type": "PRI` from a torn chunk. Add a test for that.

**Documentation:**
- Plan claims "34 tests total" in Phase 1 section — actual count is now 90+. Update when convenient.
- Volume semantics in candles: aggregator counts ticks, OANDA's REST candles count trades. Phase 6 backtesting will mix these data sources — document the difference clearly when implementing the backtester.

**Phase 1 deferred items (intentionally not implemented yet):**
- Orders with attached SL/TP (build during Phase 3 risk management)
- Date-range candle fetching (build during Phase 6 backtesting)
- List tradeable instruments via `/v3/accounts/{id}/instruments` (build during Phase 5 multi-pair work)
- Modify/cancel pending orders (build when limit orders are added)

---

## OANDA Best Practices — Future Improvements

Cross-validated by a multi-agent research review of OANDA's official docs (May 2026). Bundle A items were addressed in code; Bundles B and C below are deferred to their respective phases.

**Bundle B — Phase 3 (Risk Management):**
- **`stopLossOnFill` and `takeProfitOnFill` attached at order creation.** Atomic with the entry fill, so there's no window where a position is open without a stop. Critical for FTMO — a network failure between entry and a follow-up SL call could blow the 4% daily-loss buffer. Source: [Order Definitions](https://developer.oanda.com/rest-live-v20/order-df/).
- **`priceBound` parameter on market orders.** Caps slippage to ~3-5 pips. Without it, a news-event fill could slip 20+ pips and breach FTMO daily loss in one trade. Source: [Order Definitions](https://developer.oanda.com/rest-live-v20/order-df/).
- **`clientExtensions.id` on every order for idempotency.** After a POST timeout, instead of blindly retrying (which could double-fill), do `GET /orders/@<your_id>` first to see if the order actually landed. Without this we can't safely retry POSTs at all. Source: [Order Endpoint](https://developer.oanda.com/rest-live-v20/order-ep/).
- **`distance` vs `price` for SL/TP.** Default to `distance` for new ATR/percent-risk strategies — eliminates race conditions where the market moves while the order is in flight. Switch to `price` only when SL is computed from chart structure. Source: [Order Definitions](https://developer.oanda.com/rest-live-v20/order-df/).
- **Gate market orders on `tradeable: true`** from the price stream. OANDA's PRICE events carry a `tradeable` flag — market orders during low-liquidity windows (rollover, halts) will be rejected with `MARKET_HALTED`. Source: [Pricing Definitions](https://developer.oanda.com/rest-live-v20/pricing-df/).
- **`get_open_trades()` and `close_trade(trade_id, units="ALL")` wrappers.** Necessary for per-trade lifecycle tracking under netting (we currently only have aggregated position-level operations). Source: [Trade Endpoint](https://developer.oanda.com/rest-live-v20/trade-ep/).
- **`modify_trade_orders(trade_id, stop_loss=..., take_profit=...)`** wrapping `PUT /trades/{tradeID}/orders` for breakeven / trailing-stop management. Source: [Order Endpoint](https://developer.oanda.com/rest-live-v20/order-ep/).

**Bundle C — Phase 4 (Logging) / Phase 5 (Multi-pair) / Phase 7 (Production Hardening):**
- **`GET /accounts/{id}/changes?sinceTransactionID=X` for state sync.** OANDA's canonical incremental-sync endpoint, recommended over `/transactions/sinceid` for the main poll loop. Returns aggregated `changes` (orders/trades/positions/transactions deltas), `state` (margin, P/L, NAV), and `lastTransactionID` in one call. Three of four research agents converged on this. Source: [Best Practices](https://developer.oanda.com/rest-live-v20/best-practices/).
- **Subscribe to `/transactions/stream` for real-time fill awareness.** Production bots run TWO concurrent streams — prices for trade decisions, transactions for fill confirmations and margin-event reactions. Heartbeat carries `lastTransactionID` for backfill. Use a 20s socket timeout for this stream (transactions are lower-frequency than prices). Source: [Transaction Endpoint](https://developer.oanda.com/rest-live-v20/transaction-ep/).
- **Cache instrument metadata at startup.** Call `GET /v3/accounts/{id}/instruments` once and cache `{instrument: (displayPrecision, pipLocation, minimumTradeSize, tradeUnitsPrecision)}`. Don't hardcode 5dp — JPY pairs use 3dp. Also resolves the `mid_price` Decimal drift issue above. Source: [Primitives](https://developer.oanda.com/rest-live-v20/primitives-df/).
- **30-second VPS disconnect kill switch.** Per OANDA's official autonomous-trader guidance: if the bot loses connection to OANDA for >30s, flatten all positions automatically. Tiered defense beyond strategy-level stops. Source: [OANDA Autonomous Trader Blog](https://www.oanda.com/us-en/trade-tap-blog/trading-knowledge/the-autonomous-trader-forex-systems/).
- **Forex hours awareness: 17:00 ET Fri close → 17:05 ET Sun open, plus 6-min daily close at 16:59-17:05 ET.** Bot must close positions or widen SLs before Friday close — OANDA explicitly warns about weekend gaps blowing through stops. Source: [Hours of Operation](https://www.oanda.com/us-en/trading/hours-of-operation/).
- **Persist `lastTransactionID` to disk for crash recovery.** On restart: load it, call `/changes?sinceTransactionID=<id>`, apply deltas to local state, then reconcile any in-flight orders by looking them up via `clientExtensions.id`. Source: [Best Practices](https://developer.oanda.com/rest-live-v20/best-practices/).
- **Reconciliation on startup.** Don't trust local state alone — always issue an initial account snapshot request and compare against persisted state, falling back to full rebuild if the gap is too large. Source: [Account Endpoint](https://developer.oanda.com/rest-live-v20/account-ep/).
- **Audit log every authenticated action.** Token used, account ID, endpoint, response code, request ID, `lastTransactionID`. Required for FTMO post-incident review and dispute resolution. Source: [FTMO Forbidden Practices](https://ftmo.com/en/forbidden-trading-practices/).
- **Process supervisor (systemd / NSSM) with auto-restart.** OANDA explicitly recommends "VPS is non-negotiable" — colocate close to OANDA's servers, run 24/7, isolate from dev machines. Combine with the bot's reconcile-on-startup logic. Source: [OANDA Autonomous Trader Blog](https://www.oanda.com/us-en/trade-tap-blog/trading-knowledge/the-autonomous-trader-forex-systems/).
- **Weekend audits.** Per OANDA's official guidance: each weekend, review slippage (intended vs executed entry prices), VPS logs, and "logic drift" comparing current bot behavior to backtest expectations. Increasing slippage signals a market regime change. Source: [OANDA Autonomous Trader Blog](https://www.oanda.com/us-en/trade-tap-blog/trading-knowledge/the-autonomous-trader-forex-systems/).
- **Token rotation strategy.** OANDA doesn't retain tokens (lost = revoke + regenerate). Keep a "current + next" pattern for zero-downtime rotation. Use separate tokens for practice vs live to prevent cross-environment mistakes. Source: [Authentication](https://developer.oanda.com/rest-live-v20/authentication/).
- **FTMO 2-min news window applies to SL/TP fills too.** A stop-loss that triggers within ±2 minutes of a restricted news release counts as a breach. Risk engine needs an economic calendar feed AND SLs that won't fire during the window (or position-flatten before the window). Source: [FTMO News Trading FAQ](https://ftmo.com/en/faq/can-i-trade-news/).

**Already addressed (Bundle A):**
- `requests.Session()` for persistent connections
- 429 rate-limit retry with `Retry-After` header
- `orderRejectTransaction` detection in 2xx response bodies (`OandaOrderRejected` exception)
- Descriptive User-Agent header
- `(connect, read)` timeout tuples — longer read timeout on POST `/orders` to avoid ambiguous-state retries

---

## Phase 3 Post-Review — Bundle C (deferred)

The Phase 3 risk management code (Slices 1 + 2) was reviewed by 4 specialized analysis agents after implementation. Bundles A and B from that review have been fixed; Bundle C is deferred to later phases or low-priority cleanup.

**Defer to Phase 5 — Strategy Engine (needs market data + strategy context):**
- **Gate market orders on `tradeable: true`** from the price stream. OANDA's PRICE events carry a `tradeable` flag — market orders during low-liquidity windows (rollover, halts) are rejected with `MARKET_HALTED`. Needs the stream/aggregator to feed market state to the order layer, which only makes sense once strategy code wires it all together. Source: [Pricing Definitions](https://developer.oanda.com/rest-live-v20/pricing-df/).
- **ATR-based dynamic stop-loss.** Plan placed ATR under Phase 3 per-trade risk, but ATR is fundamentally an indicator (Phase 5 scope). Currently `position_size` accepts a static `stop_loss_pips`. When Phase 5 builds indicators, add an ATR helper and have the strategy compute SL from ATR before calling position_size.
- **`get_open_trades` / `close_trade(trade_id, units="ALL")` wrappers.** Per-trade lifecycle tracking under netting. Not needed yet because no strategy is doing per-trade management, but Phase 5 trailing-stop logic will need it. Source: [Trade Endpoint](https://developer.oanda.com/rest-live-v20/trade-ep/).
- **`modify_trade_orders(trade_id, stop_loss=..., take_profit=...)`** wrapping `PUT /trades/{tradeID}/orders` — needed for breakeven and trailing stops. Source: [Order Endpoint](https://developer.oanda.com/rest-live-v20/order-ep/).
- **`distance` vs `price` form for SL/TP.** Currently `place_market_order` always uses `distance`. London Breakout (Phase 5) will need `price` form for SL placed at the Asian range boundary. Add an alternative `stop_loss_price` parameter when that strategy is built.

**Defer to Phase 4 — Logging & Notifications:**
- **Graduated warning levels** for daily loss (e.g., 2%/3%/4% thresholds), drawdown (5%/7%/9%), and approaching daily limits. Currently only the 1,500-request warning exists. Phase 4's email alerts would naturally consume these.
- **Structured logging on every risk check.** Currently the guard prints nothing — limit breaches don't get logged anywhere. Phase 4 should add `logging.getLogger(__name__)` calls on refresh, near-limit warnings, and block events for audit/post-mortem use.
- **Audit log per authenticated action** — token used, account ID, endpoint, response code, request ID, `lastTransactionID`. Required for FTMO post-incident review.

**Defer to Phase 7 — Production Hardening:**
- **VPS disconnect kill switch.** Per OANDA's autonomous-trader guidance: if the bot loses connection >30s, auto-flatten all positions. Tiered defense beyond strategy-level stops.
- **Forex hours awareness.** 17:00 ET Friday close → 17:05 ET Sunday open. Close positions or widen SLs before Friday close to avoid weekend gaps blowing through stops. Source: [OANDA Hours of Operation](https://www.oanda.com/us-en/trading/hours-of-operation/).
- **Process supervisor + auto-restart** (systemd / NSSM). Combined with the bot's reconcile-on-startup logic — which is now in place — this lets the bot survive crashes cleanly.

**Architectural cleanup (low priority):**
- **Move pip helpers from `risk.py` to `oanda_api.py`** (or `oanda_instruments.py`). Current dependency direction is upside-down: low-level OANDA client imports from domain `risk.py` for `pip_distance`. Cleaner: instrument precision lives next to OANDA wire-format concerns.
- **Cache instrument metadata** at startup via `GET /v3/accounts/{id}/instruments`. Replace hardcoded JPY-quote heuristic with OANDA-provided `displayPrecision` and `pipLocation` per instrument. Needed before trading metals or exotics where `_is_jpy_quote` heuristic breaks.
- **Subscribe to `/transactions/stream`** for real-time fill awareness. Currently entries are tracked via `record_position_entry()` + periodic reconciliation; a transaction stream would update state instantly. Heartbeat carries `lastTransactionID` for backfill. Source: [Transaction Endpoint](https://developer.oanda.com/rest-live-v20/transaction-ep/).
- **`GET /accounts/{id}/changes?sinceTransactionID=X`** as the primary state-sync endpoint. OANDA's canonical incremental-sync API. Currently we poll `/summary` per refresh; `/changes` returns aggregated deltas in one call. Most efficient once strategy code triggers many refreshes. Source: [Best Practices](https://developer.oanda.com/rest-live-v20/best-practices/).
- **Schema versioning** for `risk_state.json`. Currently has `schema_version: 1` field stored but no migration path defined. Add explicit upgrade logic when state schema changes.

**Test coverage gaps (low priority):**
- DST transition tests in Europe/Prague (last Sunday of March and October) — verify daily rollover still fires correctly.
- Concurrent state file access tests — currently relies on `_atomic_write_json` + `RLock` correctness without end-to-end proof.
- Stress test: aggregator with hundreds of ticks across all granularities simultaneously to catch performance regressions.
- Tests for `_atomic_write_json` failure paths (disk full, permission denied, read-only filesystem).
- High position-entry-count edge cases on the reconciliation pagination boundary (OANDA caps transactions response at ~1,000).

**Phase 3 deferred per-trade items (kept for Phase 5):**
- Tradeable gating (see Phase 5 deferrals above)
- ATR-based stops (see Phase 5 deferrals above)
- Per-trade lifecycle methods (`get_open_trades`, `close_trade`, `modify_trade_orders`)
