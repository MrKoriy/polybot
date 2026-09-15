"""Weather market trader — uses free weather APIs to trade temperature/weather markets.

Polymarket has $5M+ daily volume weather markets like:
- "Will it be above 60°F in NYC on April 1?"
- "Will it rain in London on March 31?"

Edge comes from comparing free weather forecast APIs against market price.
These markets have deep liquidity — $500-2000 bets fill easily.

Uses Open-Meteo API (free, no key, unlimited) for forecasts.
Runs on its own 5-minute cycle.
"""
import asyncio
import json
import re
import time as _time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import structlog

from storage.repository import TradeRepository
from trading.portfolio import Portfolio

log = structlog.get_logger()

import math

GAMMA_API = "https://gamma-api.polymarket.com"
# Open-Meteo: free weather APIs, no key, 10K req/day shared across endpoints.
OPEN_METEO_GFS = "https://api.open-meteo.com/v1/forecast"   # GFS (NOAA)
OPEN_METEO_ECMWF = "https://api.open-meteo.com/v1/ecmwf"    # ECMWF (European)
OPEN_METEO_ICON = "https://api.open-meteo.com/v1/dwd-icon"  # ICON (DWD Germany)
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"  # historical for resolution

CHECK_INTERVAL = 300  # 5 min

# --- Signal thresholds ---
# Validation phase Apr 27: keep historical thresholds so bot generates enough
# trades to validate the new MODEL (bias correction + station-based resolution)
# in 5-7 days. Risk control is via MAX_WEATHER_BET cap, not via filter.
MIN_EDGE = 0.15              # historical baseline
MIN_BUCKET_PROB = 0.25       # historical baseline
MIN_VOLUME = 1000.0          # $1k minimum
MAX_DAYS_AHEAD = 3           # forecast beyond 3 days: σ too large, skip

# --- Sizing (re-enable validation phase, Apr 27) ---
# Hard cap $2 per trade is the main risk control — even on a 0/30 streak,
# total risk is capped at $60. Lower MIN_BET so that compute path gets through
# given the ~$48 current bankroll.
MIN_BET = 1.5
MAX_POSITION_PCT = 0.05      # historical 5%
MAX_WEATHER_BET = 2.0        # validation cap; raise to $5-7 after 30 trades
MAX_WEATHER_POSITIONS = 10   # allow multiple cities × buckets

# --- Forecast uncertainty (Celsius) by lead time ---
# Based on Open-Meteo ensemble RMSE for daily max temperature.
# https://open-meteo.com/en/docs#accuracy
_SIGMA_BY_LEAD_DAYS = {
    0: 1.0,   # today (same day): ±1°C
    1: 1.5,   # tomorrow: ±1.5°C
    2: 2.2,   # 2 days: ±2.2°C
    3: 3.0,   # 3 days: ±3°C
}

# Ensemble disagreement cutoff: if models spread > 4°C, skip (regime uncertain)
MAX_MODEL_SPREAD = 4.0

MARKET_TZ = ZoneInfo("America/New_York")

_REASON_TARGET_RE = re.compile(r"\btarget=(?P<date>\d{4}-\d{2}-\d{2})\b")
_REASON_BUCKET_RE = re.compile(r"bucket=(?P<threshold>-?\d+(?:\.\d+)?)°C")
_REASON_RANGE_RE = re.compile(
    r"range=\[(?P<low>-?\d+(?:\.\d+)?),(?P<high>-?\d+(?:\.\d+)?)\]°C"
)
_REASON_AT_OR_ABOVE_RE = re.compile(r"thr=≥(?P<threshold>-?\d+(?:\.\d+)?)°C")
_REASON_AT_OR_BELOW_RE = re.compile(r"thr=≤(?P<threshold>-?\d+(?:\.\d+)?)°C")


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf (no scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _sigma_for_lead(lead_days: int) -> float:
    """Forecast standard deviation °C as a function of lead time."""
    lead_days = max(0, min(3, lead_days))
    return _SIGMA_BY_LEAD_DAYS[lead_days]


def _prob_bucket(forecast_c: float, sigma: float,
                 low_c: float | None, high_c: float | None) -> float:
    """P(actual ∈ [low_c, high_c]) under Normal(forecast_c, sigma).

    low_c=None treats as -inf, high_c=None as +inf. Buckets are closed-closed.
    Polymarket quantises to 1°C / 2°F, so actual "bucket X" means [X-0.5, X+0.5].
    """
    if sigma <= 0:
        # deterministic
        lo_ok = (low_c is None) or (forecast_c >= low_c)
        hi_ok = (high_c is None) or (forecast_c <= high_c)
        return 1.0 if (lo_ok and hi_ok) else 0.0
    lo = -1e9 if low_c is None else (low_c - forecast_c) / sigma
    hi = 1e9 if high_c is None else (high_c - forecast_c) / sigma
    return max(0.0, _norm_cdf(hi) - _norm_cdf(lo))


def _lead_days(target_date: datetime, now: datetime) -> int:
    """Days from now to the end of target_date (rounded up)."""
    delta = (target_date.date() - now.date()).days
    return max(0, delta)

# City coordinates for weather lookup
CITY_COORDS = {
    "new york city": (40.71, -74.01),
    "new york": (40.71, -74.01),
    "nyc": (40.71, -74.01),
    "los angeles": (34.05, -118.24),
    "la": (34.05, -118.24),
    "chicago": (41.88, -87.63),
    "miami": (25.76, -80.19),
    "london": (51.51, -0.13),
    "san francisco": (37.77, -122.42),
    "sf": (37.77, -122.42),
    "washington": (38.91, -77.04),
    "dc": (38.91, -77.04),
    "dallas": (32.78, -96.80),
    "denver": (39.74, -104.99),
    "seattle": (47.61, -122.33),
    "boston": (42.36, -71.06),
    "atlanta": (33.75, -84.39),
    "phoenix": (33.45, -112.07),
    "houston": (29.76, -95.37),
    "philadelphia": (39.95, -75.17),
    # International cities from Polymarket weather markets
    "shanghai": (31.23, 121.47),
    "toronto": (43.65, -79.38),
    "seoul": (37.57, 126.98),
    "paris": (48.86, 2.35),
    "tokyo": (35.68, 139.69),
    "hong kong": (22.32, 114.17),
    "shenzhen": (22.54, 114.06),
    "singapore": (1.35, 103.82),
    "taipei": (25.03, 121.57),
    "istanbul": (41.01, 28.98),
    "beijing": (39.90, 116.40),
    "buenos aires": (-34.60, -58.38),
    "chengdu": (30.57, 104.07),
    "chongqing": (29.56, 106.55),
    "ankara": (39.93, 32.86),
    "moscow": (55.75, 37.62),
    "madrid": (40.42, -3.70),
    "rome": (41.90, 12.50),
    "berlin": (52.52, 13.41),
    "dubai": (25.20, 55.27),
    "mumbai": (19.08, 72.88),
    "delhi": (28.64, 77.22),
    "sydney": (-33.87, 151.21),
    "mexico city": (19.43, -99.13),
    "sao paulo": (-23.55, -46.63),
}

# Polymarket weather question formats:
# 1. "Will the highest temperature in Dallas be 65°F or below on April 1?"
# 2. "Will the highest temperature in Dallas be between 66-67°F on April 1?"
# 3. "Will the highest temperature in Seoul be 9°C on April 1?"
# 4. "Will the highest temperature in Seoul be 16°C or higher on April 1?"

# Pattern 1: exact temp "be X°C on date" or "be X°F or below/higher"
EXACT_TEMP_PATTERN = re.compile(
    r"highest temperature in (?P<city>[A-Za-z\s]+?) be "
    r"(?P<threshold>\d+)\s*°\s*(?P<unit>[FC])\s*"
    r"(?P<bound>or below|or higher)?"
    r".*?on (?P<date>[A-Za-z]+ \d+)",
    re.IGNORECASE,
)

# Pattern 2: range "between X-Y°F on date"
RANGE_TEMP_PATTERN = re.compile(
    r"highest temperature in (?P<city>[A-Za-z\s]+?) be "
    r"between (?P<low>\d+)-(?P<high>\d+)\s*°\s*(?P<unit>[FC])"
    r".*?on (?P<date>[A-Za-z]+ \d+)",
    re.IGNORECASE,
)


def _f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def _resolve_city(raw: str) -> str | None:
    """Match raw city name to CITY_COORDS key."""
    city = raw.strip().lower()
    if city in CITY_COORDS:
        return city
    for known in CITY_COORDS:
        if known in city or city in known:
            return known
    return None


def _parse_weather_question(question: str) -> dict | None:
    """Parse Polymarket weather question into components."""

    # Try exact temp: "be 15°C on April 1?" or "be 65°F or below on April 1?"
    m = EXACT_TEMP_PATTERN.search(question)
    if m:
        city = _resolve_city(m.group("city"))
        if not city:
            return None
        threshold = float(m.group("threshold"))
        unit = m.group("unit").upper()
        bound = (m.group("bound") or "").lower()
        threshold_c = threshold if unit == "C" else _f_to_c(threshold)

        if bound == "or below":
            # "65°F or below" — YES if temp <= 65
            return {"type": "temp_at_or_below", "city": city, "threshold_c": threshold_c,
                    "threshold_f": threshold if unit == "F" else threshold * 9/5 + 32,
                    "unit": unit,
                    "date": m.group("date").strip()}
        elif bound == "or higher":
            # "16°C or higher" — YES if temp >= 16
            return {"type": "temp_at_or_above", "city": city, "threshold_c": threshold_c,
                    "threshold_f": threshold if unit == "F" else threshold * 9/5 + 32,
                    "unit": unit,
                    "date": m.group("date").strip()}
        else:
            # Exact: "be 15°C" — YES if temp rounds to 15°C
            return {"type": "temp_exact", "city": city, "threshold_c": threshold_c,
                    "threshold_f": threshold if unit == "F" else threshold * 9/5 + 32,
                    "unit": unit,
                    "date": m.group("date").strip()}

    # Try range: "between 66-67°F on April 1?"
    m = RANGE_TEMP_PATTERN.search(question)
    if m:
        city = _resolve_city(m.group("city"))
        if not city:
            return None
        low = float(m.group("low"))
        high = float(m.group("high"))
        unit = m.group("unit").upper()
        mid = (low + high) / 2
        mid_c = mid if unit == "C" else _f_to_c(mid)
        low_c = low if unit == "C" else _f_to_c(low)
        high_c = high if unit == "C" else _f_to_c(high)
        return {"type": "temp_range", "city": city, "low_c": low_c, "high_c": high_c,
                "threshold_c": mid_c,
                "threshold_f": mid if unit == "F" else mid * 9/5 + 32,
                "unit": unit,
                "date": m.group("date").strip()}

    return None


class WeatherTrader:
    """Trades weather markets using Open-Meteo forecast API."""

    def __init__(
        self,
        portfolio: Portfolio,
        paper_mode: bool = True,
        trade_repo: TradeRepository | None = None,
        executor=None,  # OrderExecutor — required for live mode
        llm_client=None,  # LLMClient — enables anomaly filter
        breakers=None,  # CircuitBreakerManager — optional halt enforcement
        resolution=None,  # MarketResolutionService — honest paper resolution
    ) -> None:
        self._portfolio = portfolio
        self._paper_mode = paper_mode
        self._trade_repo = trade_repo
        self._executor = executor
        self._breakers = breakers
        # Honest resolution: actual market outcome, never a bot-internal oracle.
        from bot.market_resolution import MarketResolutionService
        self._resolution = resolution or MarketResolutionService()
        self._http = httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PolyBot/1.0)"},
        )
        self._running = False
        self._cycle_count = 0
        self._trades_count = 0
        self._traded_markets: set[str] = set()
        # Cache forecasts: (city, date) -> (forecast_data, timestamp)
        self._forecast_cache: dict[tuple, tuple] = {}
        # Per-position context for deterministic resolution:
        # market_id -> {"city","target_date","mtype","threshold_c"|"low_c"/"high_c","side"}
        self._pos_ctx: dict[str, dict] = {}
        # Optional LLM anomaly filter — skip trades in cities with extreme events
        # not covered by the 3-model ensemble (hurricane, polar vortex, derecho).
        self._anomaly_filter = None
        if llm_client is not None:
            from bot.weather_anomaly_filter import WeatherAnomalyFilter
            self._anomaly_filter = WeatherAnomalyFilter(llm_client)

    async def start(self) -> None:
        await self._rehydrate_position_context()
        self._running = True
        log.info("weather_trader_started")
        asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        await self._http.aclose()
        await self._resolution.close()
        log.info("weather_trader_stopped", trades=self._trades_count)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._run_cycle()
            except Exception:
                log.exception("weather_trader_error")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _run_cycle(self) -> None:
        self._cycle_count += 1

        # Resolution path must run even when halted — we still need to close out
        # old positions and realise their PnL. Only *new entries* are blocked.
        await self._resolve_expired_positions()

        if self._breakers is not None:
            halted, reason = self._breakers.is_halted("weather")
            if halted:
                log.info("weather_cycle_halted", reason=reason)
                return

        weather_positions = sum(
            1 for p in self._portfolio.open_positions
            if (p.question or "").upper().startswith("WEATHER ")
        )
        pending_weather = 0
        if self._executor is not None:
            pending_weather = sum(
                1 for data in self._executor.pending_orders
                if (data.get("question") or "").upper().startswith("WEATHER ")
            )
        weather_positions += pending_weather
        if weather_positions >= MAX_WEATHER_POSITIONS:
            log.info(
                "weather_cycle_skipped_at_cap",
                open_weather=weather_positions,
                cap=MAX_WEATHER_POSITIONS,
            )
            return

        markets = await self._find_weather_markets()
        for market in markets:
            if weather_positions >= MAX_WEATHER_POSITIONS:
                break
            traded = await self._evaluate_and_trade(market)
            if traded:
                weather_positions += 1

    async def _find_weather_markets(self) -> list[dict]:
        """Search Gamma for active weather markets via events API."""
        results = []
        seen_cids = set()
        try:
            # Weather markets are nested in events.
            # Scan events sorted by volume and extract nested markets.
            all_markets = []
            for offset in [0, 50, 100, 150]:
                resp = await self._http.get(
                    f"{GAMMA_API}/events",
                    params={
                        "closed": "false",
                        "limit": 50,
                        "offset": offset,
                        "order": "volume24hr",
                        "ascending": "false",
                    },
                )
                if resp.status_code != 200:
                    continue
                for event in resp.json():
                    slug = (event.get("slug") or "").lower()
                    title = (event.get("title") or "").lower()
                    # Quick filter on event level
                    if "temperature" in slug or "temperature" in title:
                        for m in event.get("markets", []):
                            all_markets.append(m)

            log.info("weather_markets_scanned", total=len(all_markets))

            for m in all_markets:
                    question = m.get("question", "")
                    q_lower = question.lower()

                    # Quick filter: must mention temperature keywords
                    if "highest temperature in" not in q_lower:
                        continue

                    parsed = _parse_weather_question(question)
                    if not parsed:
                        continue

                    condition_id = m.get("conditionId", "")
                    if condition_id in seen_cids:
                        continue
                    seen_cids.add(condition_id)
                    if condition_id in self._traded_markets:
                        continue
                    if condition_id in self._portfolio.held_market_ids:
                        continue
                    if self._executor and condition_id in self._executor.pending_market_ids:
                        continue

                    raw_prices = m.get("outcomePrices", '["0.5","0.5"]')
                    prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                    yes_price = float(prices[0]) if prices else 0.5

                    raw_tokens = m.get("clobTokenIds", "[]")
                    tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens

                    volume = float(m.get("volumeNum", 0) or 0)
                    if volume < 1000:
                        continue

                    results.append({
                        **parsed,
                        "condition_id": condition_id,
                        "question": question,
                        "yes_price": yes_price,
                        "no_price": 1.0 - yes_price,
                        "yes_token": str(tokens[0]) if tokens else "",
                        "no_token": str(tokens[1]) if len(tokens) > 1 else "",
                        "volume": volume,
                    })

        except Exception:
            log.exception("weather_market_search_error")

        return results

    async def _get_forecast(self, city: str, date_str: str) -> dict | None:
        """3-model ensemble forecast (GFS + ECMWF + ICON). Returns ensemble mean + σ."""
        from bot.station_weather import get_station_for_city, get_station_timezone
        station_info = get_station_for_city(city)
        if station_info:
            _, tz_name, lat, lon = station_info
        else:
            coords = CITY_COORDS.get(city)
            if not coords:
                return None
            lat, lon = coords
            tz_name = "America/New_York"

        cache_key = (city, date_str)
        cached = self._forecast_cache.get(cache_key)
        if cached and (_time.monotonic() - cached[1]) < 1800:
            return cached[0]

        # Parse date like "April 1" / "Apr 1" to YYYY-MM-DD.
        now_utc = datetime.now(timezone.utc)
        parsed_date = None
        for fmt in ["%B %d", "%b %d"]:
            try:
                parsed_date = datetime.strptime(date_str, fmt).replace(year=now_utc.year, tzinfo=timezone.utc)
                # If date already passed this year, assume next year (shouldn't happen for active markets)
                if parsed_date.date() < now_utc.date() - timedelta(days=30):
                    parsed_date = parsed_date.replace(year=now_utc.year + 1)
                break
            except ValueError:
                continue
        if parsed_date is None:
            return None
        target_yyyymmdd = parsed_date.strftime("%Y-%m-%d")
        lead_days = _lead_days(parsed_date, now_utc)
        if lead_days > MAX_DAYS_AHEAD:
            return None

        # Pull 3 independent ensembles.
        models: dict[str, float] = {}
        for name, url in [("gfs", OPEN_METEO_GFS), ("ecmwf", OPEN_METEO_ECMWF), ("icon", OPEN_METEO_ICON)]:
            try:
                resp = await self._http.get(url, params={
                    "latitude": lat, "longitude": lon,
                    "daily": "temperature_2m_max",
                    "timezone": tz_name,
                    "start_date": target_yyyymmdd, "end_date": target_yyyymmdd,
                })
                if resp.status_code != 200:
                    continue
                daily = resp.json().get("daily", {})
                vals = daily.get("temperature_2m_max", [])
                if vals and vals[0] is not None:
                    models[name] = float(vals[0])
            except Exception:
                continue

        if not models:
            return None

        vals = list(models.values())
        mean_c = sum(vals) / len(vals)
        spread = max(vals) - min(vals) if len(vals) > 1 else 0.0
        if spread > MAX_MODEL_SPREAD:
            log.info("weather_models_disagree", city=city, spread=round(spread, 2), models=models)
            return None

        # σ = model spread / 2 + base-lead uncertainty, root-sum-squared.
        base_sigma = _sigma_for_lead(lead_days)
        model_sigma = spread / 2.0
        sigma_c = math.sqrt(base_sigma ** 2 + model_sigma ** 2)

        # Per-city bias correction: Open-Meteo grid systematically differs from
        # Polymarket's resolution station (e.g. Seoul -2.75°C, NYC -1.30°C).
        # Apply bias + widen σ by adding residual σ in quadrature.
        from bot.station_weather import get_city_bias, get_city_residual_sigma
        bias = get_city_bias(city)
        residual_sigma = get_city_residual_sigma(city)
        corrected_mean = mean_c + bias
        corrected_sigma = math.sqrt(sigma_c ** 2 + residual_sigma ** 2)

        forecast = {
            "temp_max_c": corrected_mean,         # bias-corrected for station
            "sigma_c": corrected_sigma,           # widened for forecast-vs-station noise
            "raw_mean_c": mean_c,                 # for diagnostics
            "raw_sigma_c": sigma_c,
            "city_bias_c": bias,
            "lead_days": lead_days,
            "target_date": target_yyyymmdd,
            "temp_max_f": corrected_mean * 9 / 5 + 32,
            "models_used": list(models.keys()),
            "model_temps": models,
            "spread_c": spread,
            "nowcast_activated": False,
        }

        # Nowcast upgrade for same-day markets: after 14:00 local, observed
        # temperatures + remaining-hours forecast give much tighter σ than
        # the 3-model ensemble alone.
        # BUG (fixed here): the nowcast result used to REPLACE the
        # bias-corrected mean/σ wholesale — discarding the per-city station
        # bias (Seoul -2.75°C etc.) and the residual σ. Grid-based nowcast
        # values must go through the SAME station correction as the ensemble.
        if lead_days == 0:
            try:
                from bot.nowcast import fetch_nowcast
                nc = await fetch_nowcast(
                    city=city, lat=lat, lon=lon,
                    target_date_iso=target_yyyymmdd,
                    http_client=self._http,
                    ensemble_mean_c=mean_c,
                    ensemble_sigma_c=sigma_c,
                )
                if nc and nc.get("activated"):
                    nc_mean = float(nc["nowcast_mean_c"]) + bias
                    nc_sigma = math.sqrt(
                        float(nc["nowcast_sigma_c"]) ** 2 + residual_sigma ** 2
                    )
                    forecast["temp_max_c"] = nc_mean
                    forecast["sigma_c"] = nc_sigma
                    forecast["temp_max_f"] = nc_mean * 9 / 5 + 32
                    forecast["nowcast_activated"] = True
                    forecast["observed_max_so_far_c"] = nc.get("observed_max_so_far")
                    forecast["remaining_hours"] = nc.get("remaining_hours")
                    log.info(
                        "nowcast_applied",
                        city=city,
                        ensemble_mean=round(mean_c, 1),
                        ensemble_sigma=round(sigma_c, 2),
                        nowcast_mean=round(nc_mean, 1),
                        nowcast_sigma=round(nc_sigma, 2),
                        bias_c=bias,
                        residual_sigma=residual_sigma,
                        observed=nc.get("observed_max_so_far"),
                        remaining_h=nc.get("remaining_hours"),
                    )
            except Exception:
                log.exception("nowcast_error", city=city)

        self._forecast_cache[cache_key] = (forecast, _time.monotonic())
        return forecast

    async def _evaluate_and_trade(self, market: dict) -> bool:
        """Evaluate a weather market and trade if probabilistic edge exists.

        Uses ensemble forecast mean + σ with Normal CDF to compute P(bucket).
        Works for ALL market types (at_or_above / at_or_below / exact / range).
        """
        mtype = market.get("type")
        # Capital-safety filter: refuse narrow exact temperature buckets.
        # Uncertainty (±1-2°C) makes exact range betting a negative-EV lottery.
        # Only trade directional tail endpoints (at_or_above, at_or_below).
        ALLOW_EXACT_BUCKETS = False
        if not ALLOW_EXACT_BUCKETS and mtype in ("temp_exact", "temp_range"):
            log.debug("weather_skip_exact_bucket", city=market.get("city"), mtype=mtype)
            return False

        forecast = await self._get_forecast(market["city"], market["date"])
        if not forecast:
            return False

        # LLM anomaly filter: skip cities with extreme weather events under-
        # represented by the 3-model ensemble (hurricane, polar vortex, etc).
        if self._anomaly_filter is not None:
            try:
                anomaly = await self._anomaly_filter.check(
                    city=market["city"],
                    target_date=forecast["target_date"],
                    ensemble_mean_c=forecast["temp_max_c"],
                    ensemble_sigma_c=forecast["sigma_c"],
                )
                if self._anomaly_filter.should_skip(anomaly):
                    log.info(
                        "weather_trade_skipped_anomaly",
                        city=market["city"],
                        anomaly_event=anomaly.get("event"),
                        confidence=anomaly.get("confidence"),
                        reason=anomaly.get("reason"),
                    )
                    return False
            except Exception:
                log.exception("anomaly_filter_error", city=market["city"])
                # fail-open: proceed with trade

        yes_price = market["yes_price"]
        mtype = market["type"]
        mean_c = forecast["temp_max_c"]
        sigma_c = forecast["sigma_c"]

        # Capital-safety filter: refuse narrow exact temperature buckets.
        # Uncertainty (±1-2°C) makes exact range betting a negative-EV lottery.
        # Only trade directional tail endpoints (at_or_above, at_or_below).
        ALLOW_EXACT_BUCKETS = False
        if not ALLOW_EXACT_BUCKETS and mtype in ("temp_exact", "temp_range"):
            log.debug("weather_skip_exact_bucket", city=market["city"], mtype=mtype)
            return False

        is_fahrenheit = (market.get("unit") == "F")

        if is_fahrenheit:
            mean_f = mean_c * 1.8 + 32.0
            sigma_f = sigma_c * 1.8
            thr_f = market.get("threshold_f", 0.0)
            if mtype == "temp_at_or_above":
                our_prob_yes = 1.0 - _norm_cdf((thr_f - mean_f) / sigma_f) if sigma_f > 0 else (1.0 if mean_f >= thr_f else 0.0)
                reasoning = f"fcst={mean_f:.1f}°F σ={sigma_f:.2f} thr=≥{thr_f:.0f}°F"
            elif mtype == "temp_at_or_below":
                our_prob_yes = _norm_cdf((thr_f - mean_f) / sigma_f) if sigma_f > 0 else (1.0 if mean_f <= thr_f else 0.0)
                reasoning = f"fcst={mean_f:.1f}°F σ={sigma_f:.2f} thr=≤{thr_f:.0f}°F"
            else:
                return False
        else:
            EXACT_BUCKET_HALF_C = 0.5
            RANGE_PAD_C = 0.5

            if mtype == "temp_at_or_above":
                thr = market["threshold_c"]
                our_prob_yes = 1.0 - _norm_cdf((thr - mean_c) / sigma_c) if sigma_c > 0 else (1.0 if mean_c >= thr else 0.0)
                reasoning = f"fcst={mean_c:.1f}°C σ={sigma_c:.2f} thr=≥{thr:.0f}°C"

            elif mtype == "temp_at_or_below":
                thr = market["threshold_c"]
                our_prob_yes = _norm_cdf((thr - mean_c) / sigma_c) if sigma_c > 0 else (1.0 if mean_c <= thr else 0.0)
                reasoning = f"fcst={mean_c:.1f}°C σ={sigma_c:.2f} thr=≤{thr:.0f}°C"

            elif mtype == "temp_exact":
                # Bucket [thr - 0.5°C, thr + 0.5°C] (C markets are 1°C buckets)
                thr = market["threshold_c"]
                low = thr - EXACT_BUCKET_HALF_C
                high = thr + EXACT_BUCKET_HALF_C
                our_prob_yes = _prob_bucket(mean_c, sigma_c, low, high)
                reasoning = f"fcst={mean_c:.1f}°C σ={sigma_c:.2f} bucket={thr:.0f}°C [{low:.1f},{high:.1f}]"

            elif mtype == "temp_range":
                # Range [low_c - pad, high_c + pad]. pad accounts for rounding.
                low = market["low_c"] - RANGE_PAD_C
                high = market["high_c"] + RANGE_PAD_C
                our_prob_yes = _prob_bucket(mean_c, sigma_c, low, high)
                reasoning = f"fcst={mean_c:.1f}°C σ={sigma_c:.2f} range=[{low:.1f},{high:.1f}]°C"

            else:
                return False

        # Edge analysis
        yes_edge = our_prob_yes - yes_price
        no_edge = (1.0 - our_prob_yes) - (1.0 - yes_price)

        if yes_edge > MIN_EDGE and yes_edge >= no_edge and our_prob_yes >= MIN_BUCKET_PROB:
            side, token_id, price, edge = "YES", market["yes_token"], yes_price, yes_edge
        elif no_edge > MIN_EDGE and no_edge > yes_edge and (1.0 - our_prob_yes) >= MIN_BUCKET_PROB:
            side, token_id, price, edge = "NO", market["no_token"], 1.0 - yes_price, no_edge
        else:
            return False

        # Reject if fill is impractical or lopsided.
        if not token_id or price <= 0.03 or price >= 0.97:
            return False

        bankroll = self._portfolio.bankroll
        if bankroll <= 0:
            return False

        # Kelly sizing, fractional, hard-capped. Weather confidence lower than
        # crypto threshold so we stay at quarter-Kelly.
        kelly = (edge / (1.0 - price)) * 0.25
        try:
            kelly *= self._portfolio.kelly_multiplier
        except AttributeError:
            pass
        bet_size = round(min(bankroll * kelly, bankroll * MAX_POSITION_PCT, MAX_WEATHER_BET), 2)
        if bet_size < MIN_BET:
            return False

        direction = f"BUY_{side}"
        question = f"WEATHER {market['city'].title()} | {market['question'][:35]}"

        log.info(
            "weather_trade_signal",
            city=market["city"],
            type=market["type"],
            our_prob=round(our_prob_yes, 3),
            market_yes=round(yes_price, 3),
            side=side,
            edge=round(edge, 3),
            bet_size=bet_size,
            nowcast_active=forecast.get("nowcast_activated", False),
            reasoning=reasoning,
        )

        estimated_prob = our_prob_yes if side == "YES" else (1 - our_prob_yes)
        reasoning = f"{reasoning} | target={forecast['target_date']}"
        ctx = {
            "city": market["city"],
            "target_date": forecast["target_date"],
            "mtype": mtype,
            "side": side,
        }
        if mtype in ("temp_at_or_above", "temp_at_or_below", "temp_exact"):
            ctx["threshold_c"] = market["threshold_c"]
        if mtype == "temp_range":
            ctx["low_c"] = market["low_c"]
            ctx["high_c"] = market["high_c"]
        proposal_data = {
            "market_id": market["condition_id"],
            "token_id": token_id,
            "question": question,
            "direction": direction,
            "estimated_prob": estimated_prob,
            "confidence": 0.85,
            "edge": edge,
            "kelly_fraction": kelly,
            "reasoning": reasoning,
            "market_price": price,
            "metadata": {
                "strategy": "weather",
                "resolution_ctx": ctx,
            },
        }

        # --- Execution path: live and realistic-paper go through OrderExecutor.
        # Legacy paper writes a Trade directly for deterministic old backtests.
        use_executor = self._executor and (
            not self._paper_mode
            or getattr(self._executor, "realistic_paper_execution", False)
        )
        if use_executor:
            from trading.risk_manager import TradeProposal
            proposal = TradeProposal(
                market_id=market["condition_id"],
                token_id=token_id,
                question=question,
                direction=direction,
                price=price,
                size=bet_size,
                edge=edge,
                estimated_prob=estimated_prob,
                confidence=0.85,
                kelly_fraction=kelly,
                reasoning=reasoning,
                metadata=proposal_data["metadata"],
            )
            result = await self._executor.execute(proposal)
            if not result.success:
                log.warning("weather_trade_rejected",
                            city=market["city"],
                            error=result.error)
                return False
            if result.pending:
                self._traded_markets.add(market["condition_id"])
                log.info(
                    "weather_order_pending",
                    city=market["city"],
                    market_id=market["condition_id"],
                    order_id=result.order_id,
                    size=bet_size,
                )
                return True
            await self.on_live_fill(
                trade_id=result.trade_id,
                fill_price=result.fill_price,
                fill_size=result.fill_size,
                proposal_data=proposal_data,
            )
        else:
            # Paper path — simulate.
            trade_id = 0
            if self._trade_repo:
                trade = await self._trade_repo.create_trade(
                    market_id=market["condition_id"],
                    token_id=token_id,
                    question=question,
                    side="BUY",
                    direction=direction,
                    price=price,
                    size=bet_size,
                    edge=edge,
                    kelly_fraction=kelly,
                    estimated_prob=estimated_prob,
                    market_price_at_entry=price,
                    confidence=0.85,
                    status="filled",
                    paper_mode=self._paper_mode,
                    order_id=f"weather_{market['city']}_{int(_time.time())}",
                    reasoning=reasoning,
                )
                trade_id = trade.id
            self._portfolio.open_position(
                trade_id=trade_id,
                market_id=market["condition_id"],
                token_id=token_id,
                question=question,
                direction=direction,
                price=price,
                size=bet_size,
            )
            self._traded_markets.add(market["condition_id"])
            self._trades_count += 1
            self._pos_ctx[market["condition_id"]] = ctx

            log.info(
                "weather_trade_executed",
                city=market["city"],
                side=side,
                price=price,
                size=bet_size,
                edge=round(edge, 3),
                trade_id=trade_id,
                target_date=forecast["target_date"],
            )
        return True

    async def on_live_fill(
        self,
        trade_id: int,
        fill_price: float,
        fill_size: float,
        proposal_data: dict,
    ) -> None:
        if fill_price <= 0 or fill_size <= 0:
            return

        self._portfolio.open_position(
            trade_id=trade_id,
            market_id=proposal_data["market_id"],
            token_id=proposal_data["token_id"],
            question=proposal_data["question"],
            direction=proposal_data["direction"],
            price=fill_price,
            size=fill_size,
        )

        resolution_ctx = dict(
            (proposal_data.get("metadata") or {}).get("resolution_ctx") or {}
        )
        if resolution_ctx:
            self._pos_ctx[proposal_data["market_id"]] = resolution_ctx
        self._traded_markets.add(proposal_data["market_id"])
        self._trades_count += 1

        log.info(
            "weather_trade_executed",
            city=resolution_ctx.get("city"),
            side=resolution_ctx.get("side"),
            price=fill_price,
            size=fill_size,
            edge=round(proposal_data["edge"], 3),
            trade_id=trade_id,
            target_date=resolution_ctx.get("target_date"),
        )

    async def _rehydrate_position_context(self) -> None:
        """Restore weather resolution context for open positions after a restart."""
        if not self._trade_repo:
            return

        recovered = 0
        held_market_ids = self._portfolio.held_market_ids
        open_trades = await self._trade_repo.get_open_trades(self._paper_mode)

        for trade in open_trades:
            if trade.market_id in self._pos_ctx or trade.market_id not in held_market_ids:
                continue
            if not (trade.question or "").startswith("WEATHER "):
                continue

            ctx = self._recover_context_from_trade(trade)
            if not ctx:
                log.warning(
                    "weather_ctx_rehydrate_failed",
                    market_id=trade.market_id,
                    question=(trade.question or "")[:80],
                )
                continue

            self._pos_ctx[trade.market_id] = ctx
            recovered += 1

        if recovered:
            log.info("weather_ctx_rehydrated", recovered=recovered)

    def _recover_context_from_trade(self, trade) -> dict | None:
        question = trade.question or ""
        if not question.startswith("WEATHER "):
            return None

        city_part = question[len("WEATHER "):].split("|", 1)[0].strip()
        city = _resolve_city(city_part)
        if not city:
            return None

        direction = (trade.direction or "").upper()
        if direction.endswith("YES"):
            side = "YES"
        elif direction.endswith("NO"):
            side = "NO"
        else:
            return None

        target_date = self._infer_target_date(trade)
        if not target_date:
            return None

        reasoning = trade.reasoning or ""
        ctx = {
            "city": city,
            "target_date": target_date,
            "side": side,
        }

        match = _REASON_AT_OR_ABOVE_RE.search(reasoning)
        if match:
            ctx["mtype"] = "temp_at_or_above"
            ctx["threshold_c"] = float(match.group("threshold"))
            return ctx

        match = _REASON_AT_OR_BELOW_RE.search(reasoning)
        if match:
            ctx["mtype"] = "temp_at_or_below"
            ctx["threshold_c"] = float(match.group("threshold"))
            return ctx

        match = _REASON_RANGE_RE.search(reasoning)
        if match:
            ctx["mtype"] = "temp_range"
            ctx["low_c"] = float(match.group("low"))
            ctx["high_c"] = float(match.group("high"))
            return ctx

        match = _REASON_BUCKET_RE.search(reasoning)
        if match:
            ctx["mtype"] = "temp_exact"
            ctx["threshold_c"] = float(match.group("threshold"))
            return ctx

        return None

    @staticmethod
    def _infer_target_date(trade) -> str | None:
        reasoning = trade.reasoning or ""
        match = _REASON_TARGET_RE.search(reasoning)
        if match:
            return match.group("date")

        created_at = getattr(trade, "created_at", None)
        if created_at is None:
            return None
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        # Legacy trades did not persist target_date. Best-effort fallback:
        # infer the market date from the entry timestamp in the market timezone.
        return created_at.astimezone(MARKET_TZ).date().isoformat()

    async def _resolve_expired_positions(self) -> None:
        """Close weather positions at the ACTUAL market resolution.

        HISTORICAL BUG (fixed here): paper mode closed at $1/$0 based on the
        bot's own METAR station reading, which agrees with the real Polymarket
        resolution only ~86% of the time — inflating paper WR. Now:
          1. Poll the market's actual resolution (Gamma). Paper PnL comes ONLY
             from that.
          2. METAR reading is logged as a diagnostic (station vs market).
          3. LIVE closes via executor.close_live (on-chain redeem preferred).
        The old 3-day legacy write-off at $0.0 is removed — it abandoned
        real on-chain tokens in live mode and fabricated losses in paper.
        """
        now = datetime.now(timezone.utc)
        for pos in list(self._portfolio.open_positions):
            q = (pos.question or "").upper()
            if not q.startswith("WEATHER "):
                continue

            # 1. Actual market resolution — the ONLY paper PnL source.
            resolved, exit_price, note = await self._resolution.resolve_position(pos)
            if resolved and exit_price is not None:
                ctx = self._pos_ctx.get(pos.market_id, {})
                await self._close_resolved(pos, exit_price, note, ctx)
                continue

            # 2. Not yet resolved on the market — log station diagnostic only.
            ctx = self._pos_ctx.get(pos.market_id)
            if not ctx:
                log.debug("weather_no_ctx_waiting_for_resolution",
                          market_id=pos.market_id)
                continue
            try:
                target = datetime.strptime(ctx["target_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            if now < target + timedelta(hours=30):
                continue
            # Target date fully passed but market hasn't resolved yet — the
            # METAR reading tells us what we EXPECT; it never sets PnL.
            actual_c = await self._get_actual_high_c(ctx["city"], ctx["target_date"])
            if actual_c is not None:
                yes_won = self._station_implies_yes(ctx, actual_c)
                if yes_won is not None:
                    log.info(
                        "weather_station_diagnostic",
                        market=pos.question[:50],
                        city=ctx["city"],
                        actual_c=round(actual_c, 2),
                        station_implies="WIN" if yes_won else "LOSS",
                        note="market not resolved yet — waiting for actual outcome",
                    )

    def _station_implies_yes(self, ctx: dict, actual_c: float) -> bool | None:
        """What the station reading implies for the YES side (diagnostic)."""
        mtype = ctx.get("mtype")
        if mtype == "temp_at_or_above":
            return actual_c >= ctx["threshold_c"]
        if mtype == "temp_at_or_below":
            return actual_c <= ctx["threshold_c"]
        if mtype == "temp_exact":
            return abs(actual_c - ctx["threshold_c"]) <= 0.5
        if mtype == "temp_range":
            return ctx["low_c"] <= actual_c <= ctx["high_c"]
        return None

    async def _close_resolved(self, pos, exit_price: float, note: str, ctx: dict) -> None:
        if self._executor and not self._paper_mode:
            result = await self._executor.close_live(
                pos, reason=f"weather_resolved_{note}"
            )
            if not result.success:
                log.warning("weather_close_live_failed",
                            market=pos.market_id, error=result.error)
                return
            realised_exit = result.fill_price
        else:
            realised_exit = exit_price

        pnl = await self._portfolio.close_position(pos.market_id, realised_exit)
        self._pos_ctx.pop(pos.market_id, None)

        log.info(
            "weather_position_resolved",
            market=pos.question[:50],
            source=note,
            exit_price=round(exit_price, 4),
            realised_exit=round(realised_exit, 4),
            pnl=round(pnl, 2),
        )

    async def _get_actual_high_c(self, city: str, target_date: str) -> float | None:
        """Fetch recorded daily max temperature °C at Polymarket's resolution station.

        Polymarket markets resolve based on station observations (e.g.,
        LaGuardia for NYC, City Airport for London). This function uses
        bot.station_weather (METAR-backed) to match Polymarket's source.
        Validated 6/7 match against historical Polymarket lost trades.

        Retries 3x with exponential backoff. On final failure returns None —
        caller (_resolve_expired_positions) will just try again next cycle.
        """
        from bot.station_weather import get_station_max_c, get_station_for_city
        if get_station_for_city(city) is None:
            log.warning("weather_resolve_no_station_mapping", city=city)
            return None
        for attempt in range(3):
            try:
                actual = await get_station_max_c(self._http, city, target_date)
                if actual is not None:
                    return actual
                # None means valid response but no obs in window → archive not ready
                if attempt == 0:
                    log.info("weather_station_no_obs_yet",
                             city=city, date=target_date)
            except Exception as e:
                log.info("weather_station_fetch_error",
                         city=city, date=target_date,
                         attempt=attempt + 1, err=str(e)[:100])
            if attempt < 2:
                wait_s = 5 * (2 ** attempt)  # 5s, 10s, 20s
                await asyncio.sleep(wait_s)
        log.warning("weather_station_fetch_failed_after_retries",
                    city=city, date=target_date)
        return None

    def status(self) -> dict:
        return {
            "running": self._running,
            "cycles": self._cycle_count,
            "trades": self._trades_count,
        }
