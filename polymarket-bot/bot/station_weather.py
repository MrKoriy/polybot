"""Station-level weather observations for Polymarket resolution.

Polymarket weather markets resolve based on specific station readings (e.g.,
LaGuardia Airport for NYC, London City Airport for London) per their market
descriptions. Open-Meteo's city-grid output systematically differs from these
station observations by 0.5-3°C, which exceeds the bot's typical forecast σ
(~1.0-1.4°C) and was the root cause of 0/7 live WR (Apr 24-25).

This module fetches observed daily-max temperatures from aviationweather.gov's
free METAR archive — keyed by ICAO code, filtered to the local-tz day, returns
the max recorded temperature in °C.

Validated 6/7 match against Polymarket lost trades (Apr 24-25, 2026).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import structlog

log = structlog.get_logger()

METAR_API = "https://aviationweather.gov/api/data/metar"

# city (lowercase) → (ICAO code, IANA timezone, lat, lon)
# Mapping derived from Polymarket market descriptions (2026-04 sample). Update
# as new markets appear; add unrecognised cities as they're encountered.
STATIONS: dict[str, tuple[str, str, float, float]] = {
    # United States
    "new york city":  ("KLGA", "America/New_York",  40.7794, -73.8803),
    "new york":       ("KLGA", "America/New_York",  40.7794, -73.8803),
    "nyc":            ("KLGA", "America/New_York",  40.7794, -73.8803),
    "miami":          ("KMIA", "America/New_York",  25.7959, -80.2870),
    "los angeles":    ("KCQT", "America/Los_Angeles", 34.0237, -118.2913),
    "la":             ("KCQT", "America/Los_Angeles", 34.0237, -118.2913),
    "chicago":        ("KORD", "America/Chicago",   41.9786, -87.9047),
    "dallas":         ("KDFW", "America/Chicago",   32.8998, -97.0403),
    "houston":        ("KIAH", "America/Chicago",   29.9844, -95.3414),
    "boston":         ("KBOS", "America/New_York",  42.3656, -71.0096),
    "denver":         ("KDEN", "America/Denver",    39.8617, -104.6731),
    "seattle":        ("KSEA", "America/Los_Angeles", 47.4502, -122.3088),
    "phoenix":        ("KPHX", "America/Phoenix",   33.4373, -112.0078),
    "atlanta":        ("KATL", "America/New_York",  33.6407, -84.4277),
    "washington":     ("KDCA", "America/New_York",  38.8512, -77.0402),
    "dc":             ("KDCA", "America/New_York",  38.8512, -77.0402),
    "philadelphia":   ("KPHL", "America/New_York",  39.8729, -75.2437),
    "san francisco":  ("KSFO", "America/Los_Angeles", 37.6213, -122.3790),
    "sf":             ("KSFO", "America/Los_Angeles", 37.6213, -122.3790),
    # Europe
    "london":         ("EGLC", "Europe/London",     51.5048,   0.0553),
    "paris":          ("LFPB", "Europe/Paris",      48.9694,   2.4414),
    "berlin":         ("EDDB", "Europe/Berlin",     52.3667,  13.5033),
    "madrid":         ("LEMD", "Europe/Madrid",     40.4936,  -3.5668),
    "rome":           ("LIRA", "Europe/Rome",       41.7993,  12.5949),
    "moscow":         ("UUWW", "Europe/Moscow",     55.5915,  37.2615),
    "istanbul":       ("LTFM", "Europe/Istanbul",   41.2753,  28.7519),
    "ankara":         ("LTAC", "Europe/Istanbul",   40.1281,  32.9951),
    # Asia
    "tokyo":          ("RJTT", "Asia/Tokyo",        35.5494, 139.7798),
    "seoul":          ("RKSI", "Asia/Seoul",        37.4602, 126.4407),
    "shanghai":       ("ZSPD", "Asia/Shanghai",     31.1434, 121.8052),
    "shenzhen":       ("ZGSZ", "Asia/Shanghai",     22.6393, 113.8108),
    "beijing":        ("ZBAA", "Asia/Shanghai",     40.0801, 116.5846),
    "hong kong":      ("VHHH", "Asia/Hong_Kong",    22.3080, 113.9185),
    "taipei":         ("RCTP", "Asia/Taipei",       25.0777, 121.2328),
    "singapore":      ("WSSS", "Asia/Singapore",     1.3644, 103.9915),
    "chengdu":        ("ZUUU", "Asia/Shanghai",     30.5785, 103.9471),
    "chongqing":      ("ZUCK", "Asia/Shanghai",     29.7192, 106.6416),
    "delhi":          ("VIDP", "Asia/Kolkata",      28.5562,  77.1000),
    "mumbai":         ("VABB", "Asia/Kolkata",      19.0887,  72.8679),
    "dubai":          ("OMDB", "Asia/Dubai",        25.2532,  55.3657),
    # Oceania / South America
    "sydney":         ("YSSY", "Australia/Sydney", -33.9461, 151.1772),
    "buenos aires":   ("SAEZ", "America/Argentina/Buenos_Aires", -34.8222, -58.5358),
    "sao paulo":      ("SBGR", "America/Sao_Paulo", -23.4356, -46.4731),
    "mexico city":    ("MMMX", "America/Mexico_City", 19.4363, -99.0721),
}

DEFAULT_TIMEOUT_S = 20.0
QUERY_HOURS = 36  # buffer ensures full local day coverage


def get_station_for_city(city: str) -> tuple[str, str, float, float] | None:
    """Look up Polymarket resolution station for a city. Returns (icao, tz, lat, lon) or None."""
    return STATIONS.get(city.lower().strip())


# Per-city bias = mean(station_actual - openmeteo_grid_forecast) over 14 days.
# Computed via scripts/weather_backtest.py on 2026-04-13..26 sample.
# Apply: corrected_forecast = openmeteo_forecast + CITY_BIAS[city]
# Re-run backtest monthly to re-calibrate.
CITY_BIAS: dict[str, float] = {
    "berlin":         +0.29,
    "hong kong":      +1.59,
    "london":         -0.17,
    "madrid":         +0.14,
    "miami":          +0.91,
    "moscow":         -0.53,
    "new york city":  -1.30,
    "nyc":            -1.30,
    "new york":       -1.30,
    "paris":          -0.03,
    "seoul":          -2.75,
    "shanghai":       -0.94,
    "sydney":         +0.32,
    "tokyo":          -0.86,
}

# Per-city stdev of the residual — added in quadrature to forecast σ to account
# for forecast-vs-station noise. Computed from same backtest.
CITY_RESIDUAL_SIGMA: dict[str, float] = {
    "berlin":         0.87,
    "hong kong":      0.73,
    "london":         0.73,
    "madrid":         0.74,
    "miami":          0.58,
    "moscow":         1.31,
    "new york city":  1.22,
    "nyc":            1.22,
    "new york":       1.22,
    "paris":          0.41,
    "seoul":          1.50,
    "shanghai":       1.13,
    "sydney":         0.86,
    "tokyo":          0.86,
}

# Default for unmapped cities — overall observed residual σ
DEFAULT_RESIDUAL_SIGMA = 1.40


def get_city_bias(city: str) -> float:
    """Mean station-vs-grid residual (°C). 0 if unknown."""
    return CITY_BIAS.get(city.lower().strip(), 0.0)


def get_city_residual_sigma(city: str) -> float:
    """Per-city forecast-vs-station residual σ. Default 1.40°C."""
    return CITY_RESIDUAL_SIGMA.get(city.lower().strip(), DEFAULT_RESIDUAL_SIGMA)


async def get_station_max_c(
    client: httpx.AsyncClient,
    city: str,
    target_date_iso: str,
) -> float | None:
    """Fetch max temperature in °C at city's resolution station on target_date_iso (YYYY-MM-DD).

    target_date_iso is interpreted in the station's local timezone (Polymarket
    markets typically resolve on local-day basis). Returns None if station not
    mapped, API call fails, or no observations within the day.
    """
    info = get_station_for_city(city)
    if info is None:
        log.warning("station_unknown_city", city=city)
        return None
    icao, tz_name, _lat, _lon = info
    LOCAL = ZoneInfo(tz_name)

    try:
        start_local = datetime.strptime(target_date_iso, "%Y-%m-%d").replace(tzinfo=LOCAL)
    except ValueError:
        log.warning("station_bad_date", city=city, date=target_date_iso)
        return None
    end_local = start_local + timedelta(days=1)

    # METAR API's `date` param is the END of the rolling window (returns
    # `hours` worth of observations BEFORE that timestamp).
    end_utc_buffered = end_local.astimezone(timezone.utc) + timedelta(hours=4)
    query_str = end_utc_buffered.strftime("%Y%m%d_%H%M")

    try:
        resp = await client.get(METAR_API, params={
            "ids": icao,
            "format": "json",
            "date": query_str,
            "hours": QUERY_HOURS,
        })
    except Exception as e:
        log.warning("station_http_error", city=city, icao=icao, err=str(e)[:120])
        return None

    if resp.status_code != 200:
        log.warning("station_bad_status", city=city, icao=icao,
                    status=resp.status_code)
        return None

    try:
        data = resp.json() or []
    except Exception:
        return None

    temps_in_day: list[float] = []
    for obs in data:
        ts = obs.get("obsTime")
        temp = obs.get("temp")
        if ts is None or temp is None:
            continue
        try:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LOCAL)
        except Exception:
            continue
        if start_local <= dt < end_local:
            try:
                temps_in_day.append(float(temp))
            except (TypeError, ValueError):
                continue

    if not temps_in_day:
        log.info("station_no_obs_in_day", city=city, icao=icao,
                 date=target_date_iso, raw_obs=len(data))
        return None

    max_c = max(temps_in_day)
    log.debug("station_max_obs", city=city, icao=icao,
              date=target_date_iso, max_c=round(max_c, 2),
              n_obs=len(temps_in_day))
    return max_c
