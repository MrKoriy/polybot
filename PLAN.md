# PLAN: Polybot Weather Oracle & Maker Architecture Overhaul

## 1. Architectural Strategy & Invariants
- **No Synthetic Oracles**: PnL is recognized only on confirmed Gamma settlement (`market.resolved == True`) or executable CLOB book fills.
- **Station-Accurate Forecasting**: All Open-Meteo ensemble calls must use exact station coordinates (airport runway / observatory), never generic city centroids.
- **Native Unit Arithmetic**: Markets specifying Fahrenheit (NOAA US stations) must compute normal distribution CDF in native °F (`sigma_f = sigma_c * 1.8`, `mean_f = mean_c * 1.8 + 32`) to eliminate bucket boundary distortion.
- **Binary Endpoints Only**: Reject narrow exact temperature ranges (`kind == "range"`). Trade exclusively directional tail endpoints (`at_or_above`, `at_or_below`).
- **Maker-Only Liquidity & Complete Sets**: Enforce 0% taker fee regime by focusing on passive maker quotes and complete set collateral merging (`complete_set_maker.py`).

## 2. File Modularity & Assignments

### Wave 1: Weather Oracle & Station Enhancement
- **Files**:
  - `polymarket-bot/bot/station_weather.py`
  - `polymarket-bot/bot/weather_trader.py`
- **Depends-on**: `none`
- **Changes**:
  1. In `station_weather.py`:
     - Update `STATIONS` dictionary with native unit (`unit: "F" | "C"`), agency (`NOAA`, `HKO`, etc.), and exact airport/observatory coordinates.
     - Add helper `parse_resolution_source(description: str) -> dict` to extract station code, native unit, and resolving authority from Polymarket Gamma descriptions.
  2. In `weather_trader.py`:
     - Disallow exact range buckets (`ENABLE_EXACT_BUCKET_BETS = False`). Only allow `at_or_above` and `at_or_below`.
     - When market uses °F, evaluate probability directly in °F: convert ensemble distribution once (`mean_f = mean_c * 1.8 + 32`, `sigma_f = sigma_c * 1.8`), then compute `norm.cdf(target_f, mean_f, sigma_f)`.
     - Query Open-Meteo using `station_lat` and `station_lon` instead of city center coordinates.

### Wave 2: Test Suite & Deprecation Hygiene
- **Files**:
  - `polymarket-bot/tests/unit/test_station_weather.py`
  - `polymarket-bot/tests/unit/test_weather_trader.py` (or `test_weather_context.py`)
  - `polymarket-bot/trading/portfolio.py`
  - `polymarket-bot/storage/repository.py`
- **Depends-on**: `Wave 1`
- **Changes**:
  1. Add unit tests verifying station metadata, resolution source parsing, native unit detection, and Fahrenheit CDF calculation.
  2. Verify that exact temperature buckets are rejected with clear log reasons.
  3. Clean up `datetime.utcnow()` deprecation warnings in touched files using `datetime.now(timezone.utc)`.

### Wave 3: Quality Gates, Smoke Verification & Live Paper Launch
- **Files**:
  - `polymarket-bot/scripts/check_live_ready.py`
  - `polymarket-bot/scripts/start_realistic_paper.sh`
- **Depends-on**: `Wave 2`
- **Changes**:
  1. Run full test suite: `uv run --extra dev pytest` (100% green required).
  2. Test execution of `check_live_ready.py` and `scripts/start_realistic_paper.sh`.
  3. Validate real-time orderbook reading and Gamma API resolution checks.
