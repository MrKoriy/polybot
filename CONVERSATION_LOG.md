# Polymarket AI Trading Bot — Full Development Log

## Project: Polymarket Paper Trading Bot
**Period:** March 21-29, 2026
**Starting bankroll:** $10 (paper mode)
**Peak bankroll:** $24.98
**Current bankroll:** ~$11 (after fixing resolution bug that caused silent drain)

---

## Phase 1: Initial Analysis & MiroFish Research (March 21)

### What was done:
- Full codebase audit of the existing Polymarket trading bot
- Research of MiroFish repository (multi-agent swarm intelligence prediction engine)
- **Decision: NOT to integrate MiroFish** — too slow (minutes per simulation) for Polymarket where edge window is 30 sec - 5 min
- Identified that "News Edge" strategy (current bot) is more practical

### MiroFish findings:
- Open-source multi-agent social simulation (38.2k GitHub stars)
- Generates predictions via simulated Twitter/Reddit interactions
- High latency, qualitative outputs — unsuitable for real-time trading
- Could be useful later for slow political markets

---

## Phase 2: Critical Bug Fixes — Feedback Loops (March 21)

### 5 critical fixes that enabled the bot to actually trade:

| # | Fix | Impact |
|---|---|---|
| 1 | **Wired calibration → bias correction** | Bot now learns from its prediction errors |
| 2 | **Fixed Brier score in AutoResearch** | Prompt experiments can now see real metrics |
| 3 | **Lowered meta-strategy threshold** (0.6→0.4) | 70% more insights are now applied |
| 4 | **Dynamic slippage** (25% of edge vs fixed 2%) | Stops giving away half the profit on small edges |
| 5 | **Patience exit logic** (4-6h vs 2h) | Positions get time to materialize |

---

## Phase 3: Root Cause — Why Bot Never Traded (March 21)

### Discovery: Paper mode couldn't get prices!

**Root cause chain:**
1. `get_midpoint()` returned `None` in paper mode (required CLOB client auth)
2. `get_market_price_gamma()` used wrong endpoint (`/markets/{id}` — Gamma expects numeric ID)
3. Positions opened but couldn't close → accumulated → blocked new trades via `max_positions`
4. 417 risk events of `max_positions` over 6 days, 0 real trades

### Fixes:
- Added public CLOB REST fallback: `clob.polymarket.com/midpoint?token_id=...`
- Fixed Gamma API to use query param `?conditionId=...` instead of path
- Both work without authentication — perfect for paper mode

---

## Phase 4: Comprehensive Bug Audit (March 21-22)

### 4 parallel audit agents found ~80 bugs across entire codebase

**Critical (7 fixed):**
- WebSocket division by zero on price=0
- Edge detector division by zero on extreme prices
- Kelly calculator no bankroll≤0 check
- Portfolio allows negative bankroll
- LLM empty response causes IndexError
- **Wrong EV formula** — was giving negative EV on positive edge trades
- Breakeven trades counted as losses

**High (9 fixed):**
- Settings mutated mid-trade by AutoResearch
- Module-level prompts mutated globally
- sync_from_db doesn't restore peak_bankroll/daily_pnl
- No trading fees accounted for
- Executor results not validated (fill_size=0 passes)
- Duplicate category classification functions
- Balance int() truncation loses cents
- GDELT unparseable dates faked as "now"
- No settings validation

**Medium (15 fixed):**
- Brave search fragile age parsing ("1-2 hours" → parsed as 12)
- Finnhub division by zero on empty query
- Twitter rate_limit_on disabled
- News dedup by first 60 chars — misses duplicates
- Cache hash only first 10 items
- Geometric mean confidence=0 when either model is 0
- Experiment log no file lock
- AutoResearch starts before pipeline ready
- Shutdown race condition
- And more...

---

## Phase 5: First Trade! (March 22)

### Problem: LLM anchored to market price
- Prompt said "3-5% deviation is a STRONG claim" — effectively forbidden LLM from disagreeing
- All estimates within 0-2% of market price → no edge found

### Solution: Rewrote prompts to encourage independence
- "Markets are often SLOW to react and frequently mispriced by 5-15%"
- "DO NOT anchor to market price. The market can be wrong."
- Market price moved to step 4 (compare) instead of step 1 (start here)

### Result: First trades within 1 cycle
- UCLA Bruins vs. Connecticut Huskies: BUY_NO, edge 4.66%
- Iowa Hawkeyes vs. Florida Gators: BUY_YES, edge 12.7%

---

## Phase 6: Server Deployment — Germany (March 22)

### Setup:
- Server: 45.146.167.109 (Germany)
- Systemd service with auto-restart
- Dashboard at port 8050

### Problem: All news sources broken on VPS
- Reddit: 403 (IP blocked)
- Brave: Monthly quota exhausted (2001/2000)
- GDELT: Persistent rate limit

### Solution: Added Google News RSS
- Free, no API key, no rate limits
- Works perfectly on VPS
- Provides 30-100 articles per query

---

## Phase 7: Model Optimization (March 22)

### Benchmarked 4 models on OpenRouter:

| Model | Speed | Cost (input/output per 1M) | Quality |
|---|---|---|---|
| **GPT-5.4 Nano** | 2.0s | $0.20/$1.25 | Best — not overconfident |
| Gemini 2.5 Flash | 2.5s | $0.30/$2.50 | Good but confused on test |
| Gemini 3.1 Flash Lite | 3.0s | $0.25/$1.50 | Overconfident (0.85 confidence) |
| MiniMax M2.7 | FAIL | $0.30/$1.20 | Crashed |

### Decision: GPT-5.4 Nano (primary) + Gemini 3.1 Flash Lite (secondary ensemble)
- 33-50% cheaper than Gemini 2.5 Flash
- 20% faster
- Less overconfident → fewer false signals

---

## Phase 8: BTC 15-Minute Trading (March 22)

### Built BTC Up/Down trader:
- Monitors BTC momentum from Binance (1m candles, RSI, volume)
- Finds current 15-min market on Polymarket via slug pattern `btc-updown-15m-{timestamp}`
- Auto-transitions to next market every 15 minutes
- First trade within minutes of deployment

### Later expanded to 5-minute markets + multi-timeframe

---

## Phase 9: AutoResearch Breakthrough — $10→$40 (March 27)

### What AutoResearch discovered (70+ experiments):

| Experiment | PnL | Description |
|---|---|---|
| **R0035_contrarian** | **+$6.00** | "Look for overreactions and fade the crowd" — KEY breakthrough |
| P0067 | +$4.78 | top_markets_to_analyze: 28→33 |
| R0036_direct | +$1.71 | Shorter prompts → more precise LLM |
| P0065-P0068 | +$1.50 | max_position_pct optimized to 0.30 |

### Autotuned parameters (saved to .env):
```
MIN_CONFIDENCE=0.5
KELLY_FRACTION=0.1
TOP_MARKETS_TO_ANALYZE=38
MIN_EDGE=0.11
NEWS_LOOKBACK_HOURS=16
LLM_BIAS_CORRECTION=0.02
MAX_POSITION_PCT=0.3
```

### Auto-save cron: every 2 days at 04:00 UTC

---

## Phase 10: Multi-Crypto + Copy Trading + ESPN (March 27)

### Added BTC + ETH + SOL trading (5m + 15m = 6 parallel markets)
- Binance REST for momentum data
- Each crypto × timeframe traded independently
- Fee filter: don't trade if taker fee (1.8%) > expected edge

### Added Copy Trading module:
- 8 verified seed wallets (reachingthesky, LucasMeow, Tsybka, RN1, Theo4, etc.)
- Auto-discovers new whales from $100+ trades
- Wallet basket consensus: 2+ wallets agree → signal
- Scans every 30 seconds

### Added ESPN Sports Data:
- Free API, no key needed
- Team records, recent form (last 5 games), head-to-head, injuries
- Spreads and O/U from ESPN odds
- Connected as primary news source for sports markets

---

## Phase 11: NYC VPS Migration (March 27)

### Moved from Germany to New York for low latency:

| Metric | Germany | NYC |
|---|---|---|
| Polymarket latency | 80-120ms | **2.5ms** |
| Binance latency | 100-150ms | **1.7ms** |

### Added new modules:
- **Binance WebSocket** — real-time BTC/ETH/SOL prices (sub-ms)
- **Spread Capture** — buys both Up+Down when mispriced (Gabagool strategy)
- **Last-Second** — bets on winning side 10-15 sec before resolution
- **Fee calculation** — dynamic taker fee check before every crypto trade

### Server: 172.86.105.159 (NYC)
### Dashboard: http://172.86.105.159:8050

### Fixed Binance US block:
- `stream.binance.com` returns HTTP 451 from US IPs
- Switched to `data-stream.binance.vision` (works from US)
- REST: `data-api.binance.vision/api/v3` instead of `api.binance.com`

### Fixed Starlette 1.0 compatibility:
- `TemplateResponse` API changed in Starlette 1.0 (Python 3.13)
- Updated all 5 template calls to new `TemplateResponse(request, template, context)` format

---

## Phase 12: Position Resolution Bug Fix (March 28-29)

### Problem: 51 stuck positions, bankroll draining silently

**Root cause:** Crypto positions closed with `exit_price=0.5` instead of real resolution.
- Gamma API often doesn't update `resolved=true` within 7 minutes
- Every trade lost $0.005 (entry ~0.505 - exit 0.500)
- 3500 trades × $0.005 = $17.50 lost silently
- Win rate showed 45% but real P&L was negative

### Fix: Binance-based resolution
- Record Binance price at entry time
- At resolution, compare current Binance price with entry price
- If BTC went up and we bet Up → `exit_price=1.0` (WIN)
- If BTC went down and we bet Up → `exit_price=0.0` (LOSS)
- No more 0.5 fallback — real wins and real losses

### Also fixed:
- `TypeError: can't subtract offset-naive and offset-aware datetimes` (crashed every 5 sec)
- Autotuned params not migrated to NYC .env
- Daily Loss Used metric now shows gross losses, not just net P&L

---

## Phase 13: Full Market Analysis (March 29)

### Analyzed all 247 active Polymarket events:

**Unrealized opportunities (not yet implemented):**

| Category | 24h Volume | Strategy | Expected daily ($300) |
|---|---|---|---|
| "BTC above $X on date" | $3M+ | Binance price vs threshold | +$15-30 |
| Weather markets | $5M+ | Weather API forecast | +$10-25 |
| Elon tweet count | $5M+ | Twitter API count | +$5-15 |

**Current bot runs 5 modules in parallel:**
1. Main pipeline (LLM news edge — sports, politics, esports)
2. Crypto momentum (BTC/ETH/SOL × 5m/15m)
3. Spread capture + last-second
4. Copy trading (8 seed + auto-discovery)
5. AutoResearch (continuous optimization)

---

## Architecture Summary

```
polymarket-bot/
├── analysis/
│   ├── probability_estimator.py  — Ensemble LLM estimation
│   ├── edge_detector.py          — Mispricing detection
│   ├── llm_client.py             — OpenRouter/Claude/OpenAI
│   ├── calibration_tracker.py    — Brier score tracking
│   ├── market_filter.py          — Market selection
│   └── prompts.py                — LLM prompts (autotuned)
├── bot/
│   ├── app.py                    — Main application orchestrator
│   ├── pipeline.py               — News edge trading loop
│   ├── btc_trader.py             — Multi-crypto momentum (BTC/ETH/SOL)
│   ├── spread_capture.py         — Spread capture + last-second
│   ├── copy_trader.py            — Whale wallet basket consensus
│   └── health.py                 — Health monitoring
├── data/
│   ├── binance_ws.py             — Binance WebSocket feed
│   ├── btc_price.py              — Binance REST price feed
│   ├── google_news_source.py     — Google News RSS
│   ├── espn_source.py            — ESPN sports data
│   ├── polymarket_rest.py        — Polymarket CLOB + Gamma API
│   ├── polymarket_ws.py          — Polymarket WebSocket
│   ├── wallet_tracker.py         — Whale wallet monitoring
│   ├── brave_search.py           — Brave Search (backup)
│   ├── gdelt_source.py           — GDELT global news
│   ├── reddit_source.py          — Reddit JSON endpoints
│   ├── finnhub_source.py         — Finnhub financial data
│   └── twitter_source.py         — Twitter/X via tweepy
├── trading/
│   ├── executor.py               — Order execution (FOK/GTC)
│   ├── kelly.py                  — Kelly Criterion sizing
│   ├── risk_manager.py           — Risk checks + daily limits
│   └── portfolio.py              — Position tracking + P&L
├── research/
│   ├── runner.py                 — AutoResearch orchestrator
│   ├── autotuner.py              — Level 1: parameter optimization
│   ├── prompt_optimizer.py       — Level 2: prompt A/B testing
│   ├── meta_strategy.py          — Level 3: pattern discovery
│   └── experiment_log.py         — TSV experiment logging
├── storage/
│   ├── database.py               — Async SQLite
│   ├── models.py                 — ORM models
│   └── repository.py             — Data access layer
├── config/
│   ├── settings.py               — Pydantic settings + validation
│   ├── constants.py              — Enums
│   └── logging_config.py         — Structlog setup
├── web/
│   ├── server.py                 — FastAPI dashboard
│   └── templates/                — Jinja2 templates
├── notifications/
│   └── telegram.py               — Telegram bot
├── core/
│   ├── event_bus.py              — Pub/sub events
│   ├── scheduler.py              — APScheduler wrapper
│   └── exceptions.py             — Custom exceptions
├── scripts/
│   └── save_snapshot.sh          — Auto-save parameters (cron)
└── tests/                        — 88 tests (unit + integration)
```

---

## Servers

| Server | IP | Purpose | Status |
|---|---|---|---|
| Germany | 45.146.167.109 | Original (stopped) | Disabled |
| **NYC** | **172.86.105.159** | **Production** | **Active** |

### Dashboard: http://172.86.105.159:8050

### Monitoring commands:
```bash
# Live logs
journalctl -u polymarket-bot -f

# Status
systemctl status polymarket-bot

# Restart
systemctl restart polymarket-bot

# Save params
bash /root/polymarket-bot/scripts/save_snapshot.sh
```

---

## Key Learnings

1. **LLM anchors to market price** if you tell it to — prompts must encourage independence
2. **AutoResearch works** — contrarian prompt was the breakthrough ($10→$24)
3. **Paper mode ≠ live** — no fees, no slippage, no partial fills. Expect 40-75% of paper results in live
4. **Taker fees (1.8%)** kill crypto momentum profit — must use maker orders (0% fee) for live
5. **Resolution price matters** — exit_price=0.5 causes silent drain of ~$0.005/trade
6. **US VPS blocks Binance** — use `data-stream.binance.vision` and `data-api.binance.vision`
7. **ESPN + Google News** are the most reliable free data sources on VPS
8. **Starlette 1.0** changed TemplateResponse API — breaks on Python 3.13

---

## Total bugs fixed: 50+
## Total tests passing: 88
## Total modules: 5 parallel trading strategies
## Total lines of code added/modified: ~3000+
