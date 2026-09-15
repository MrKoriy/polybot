# BRIEF: Polybot Weather & Market-Making Overhaul

## 1. Executive Summary
Transform `polybot` from a loss-making multi-strategy experiment into a mathematically sound, capital-safe Polymarket trading system. Eliminate dead retail taker strategies (Crypto 5m/15m RSI scalping, 30s latency copy-trading). Fix the Weather strategy to strictly align with Polymarket's official airport/observatory resolution oracles and native units (°F vs °C). Deploy and verify passive complete-set maker arbitrage and honest realistic paper trading.

## 2. Core Problems Being Solved
1. **Weather Resolution & Station Mismatch**:
   - Polymarket resolves daily temperature markets based on specific airport stations (e.g. NOAA at LaGuardia `KLGA` for NYC in °F, Hong Kong Observatory in °C, Shanghai Pudong `ZSPD` in °C).
   - Prior code used city-grid coordinates (causing 0.8°C–2.5°C systematic bias) and hourly METAR reports (which miss intra-hour daily max peaks recorded by continuous ASOS/NOAA sensors).
   - Rounding between °C and °F caused off-by-one errors on exact temperature bucket boundaries.
2. **Dead Taker Strategies**:
   - 5m/15m BTC scalping lost money due to Polymarket taker fees (~1.8%), dynamic fees on short-duration markets, crossing 2–5¢ spreads, and lagging RSI indicators.
   - Copy-trading suffered from 30s polling lag, adverse selection, and copying single legs of hedged whale positions.
3. **Paper-Trading Self-Deception**:
   - Legacy code resolved paper positions using an internal Binance/METAR oracle with synthetic $1.00/$0.00 payouts rather than actual Polymarket Gamma resolutions, creating a fake "100% WR" track record.

## 3. Scope of Work & Deliverables
1. **Weather Module Upgrade (`bot/weather_trader.py`, `bot/station_weather.py`)**:
   - Parse resolution rules, station ICAO/WBAN, and native units (°F vs °C) directly from Polymarket Gamma market descriptions.
   - Multi-model ensemble (GFS + ECMWF + ICON) queried at the exact station coordinates (airport runways/observatories), not city centroids.
   - Native Fahrenheit calculations for US markets (NYC, Miami, Chicago, etc.) without lossy round-trip conversions.
   - Trade ONLY above/below endpoints and wide tails (`at_or_above`, `at_or_below`); hard-reject narrow exact temperature buckets (`range`).
   - Honest resolution tracking against actual Gamma market settlements.
2. **Complete Set & Spread Capture Hardening (`bot/complete_set_maker.py`)**:
   - Pure post-only (Maker) execution on liquid binary markets.
   - Merges complementary outcome tokens (YES + NO) when sum of costs + maker rebates < $0.99 for guaranteed profit.
3. **Deprecation Clean-up**:
   - Permanently keep `btc_trader` and `copy_trader` disabled in `bot/app.py` and settings.
4. **Realistic Paper Verification & Test Suite**:
   - 100% green test suite across all unit and integration tests.
   - Validated launch of `start_realistic_paper.sh` and web dashboard on local port.

## 4. Hard Constraints
- Max lines per file <= 350 where feasible; modular responsibilities.
- Strict typing, zero unhandled exceptions, zero fake oracles.
- All paper trades must resolve via Gamma settlement or executable CLOB book depth.
