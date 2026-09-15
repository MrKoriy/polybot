# Polymarket Bot — Research Summary

## Status: ~85-90% implemented. Core trading loop works in paper mode.

## Critical Gaps

### 1. Live Trading Execution (PRIORITY 1)
- `executor._live_execute()` is stubbed — paper mode only
- Missing: signature_type param, tick_size per market, negRisk flag handling
- py-clob-client calls are synchronous — must wrap in `asyncio.to_thread()`
- **CRITICAL**: Open GTC orders require heartbeat every 5 seconds or they auto-cancel after 10s
- For small bets ($10 start), use FOK market orders — simpler, no heartbeat needed
- Minimum order: 5 shares for limit orders
- Must set up token allowances (USDC + CTF approve to 3 exchange contracts)

### 2. UI Improvements (PRIORITY 2)
- Add calibration plot (scatter chart: predicted vs actual probability)
- Add risk metrics row (Sharpe, Max DD progress bar, Brier Score, Daily Loss usage)
- Chart.js: update in-place instead of destroy/recreate
- Add responsive CSS breakpoints for mobile
- Add drawdown overlay on equity chart
- Add time range selector (1D/1W/1M/ALL)
- Consider SSE (sse-starlette) instead of HTMX polling for real-time data

### 3. Data Sources (PRIORITY 3)
- Twitter/X not implemented (plan called for it)
- GDELT Project: free, 15-min entity-level trend detection globally — consider adding
- Brave Search already has API key — verify it's working well
- Finnhub needs API key — currently empty in .env
- For crypto markets: add Binance/Coinbase WebSocket for latency arbitrage

### 4. Prediction Quality (PRIORITY 4)
- Use ensemble of 3+ models (currently dual-model)
- Apply temperature scaling for calibration post-hoc
- Track Brier scores per category (politics, sports, crypto separately)
- Use chain-of-thought reasoning in prompts
- Add "superforecasting commandments" to system prompt
- Current min_edge 0.04 is good; min_confidence 0.60 might be low (research suggests 0.75)

## Key Strategic Findings

- 14 of 20 most profitable Polymarket wallets are bots (~$40M total)
- Average arbitrage opportunity: 2.7 seconds (down from 12.3s in 2024)
- Most profitable for bots: 15-min crypto contracts (latency arbitrage vs exchanges)
- 1/4 Kelly is the recommended fraction (current setting: 0.25 — good)
- LLM ensemble of 12 models matched human crowd accuracy (Science Advances 2025)
- Never exceed 5% of bankroll per market

## Tech Debt
- Sync py-clob-client calls in async context (will block event loop)
- CalibrationTracker needs Brier score computation
- No integration tests (respx available for HTTP mocking)
- prompt_optimizer.py (L2 AutoResearch) needs completion
