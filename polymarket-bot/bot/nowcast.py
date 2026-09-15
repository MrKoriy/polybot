"""Nowcasting for same-day weather markets.

For markets resolving TODAY, after ~14:00 local time the daily max temperature
is largely determined. We fetch observed hourly temperatures from Open-Meteo
(past_days=1) and combine them with the remaining forecast hours to produce a
much tighter nowcast estimate than the baseline 3-model ensemble.

This is a pure augmentation — if conditions for nowcasting are not met
(lead_days>0, too early in day, bad data), the caller falls back to the
ensemble unchanged.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import structlog

log = structlog.get_logger()

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"

# Only activate nowcast after this local hour — daily max usually occurs
# between 14:00 and 18:00 local time.
NOWCAST_ACTIVE_LOCAL_HOUR = 14

# If observed max so far exceeds the ensemble forecast mean by more than this,
# we treat the reading as a bad station / API glitch and refuse to nowcast.
OBSERVED_ANOMALY_BOUND_C = 2.0

# Floor on shrunken sigma — never claim sub-0.2°C uncertainty.
MIN_NOWCAST_SIGMA_C = 0.2

# Baseline "full day" hours for sigma shrinkage calculation.
_HOURS_PER_DAY = 24.0

# Timezone used for market cutoff. Per-city map — the daily max refers to the
# LOCAL day of the market's city, so observed/remaining splits must be
# computed in the city's timezone. Previously America/New_York was hardcoded,
# which shifted Asian/European same-day splits by 9-14 hours (wrong-day data).
_DEFAULT_TZ = ZoneInfo("America/New_York")

CITY_TZ = {
    # US cities default to their actual zones
    "new york city": "America/New_York", "new york": "America/New_York",
    "nyc": "America/New_York", "boston": "America/New_York",
    "philadelphia": "America/New_York", "washington": "America/New_York",
    "dc": "America/New_York", "miami": "America/New_York",
    "atlanta": "America/New_York",
    "chicago": "America/Chicago", "dallas": "America/Chicago",
    "houston": "America/Chicago",
    "denver": "America/Denver",
    "phoenix": "America/Phoenix",
    "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "sf": "America/Los_Angeles",
    "seattle": "America/Los_Angeles",
    # International
    "london": "Europe/London", "paris": "Europe/Paris",
    "madrid": "Europe/Madrid", "rome": "Europe/Rome",
    "berlin": "Europe/Berlin", "moscow": "Europe/Moscow",
    "istanbul": "Europe/Istanbul", "ankara": "Europe/Istanbul",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "shanghai": "Asia/Shanghai", "beijing": "Asia/Shanghai",
    "shenzhen": "Asia/Shanghai", "chengdu": "Asia/Shanghai",
    "chongqing": "Asia/Shanghai", "hong kong": "Asia/Hong_Kong",
    "taipei": "Asia/Taipei", "singapore": "Asia/Singapore",
    "dubai": "Asia/Dubai", "mumbai": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "sydney": "Australia/Sydney",
    "toronto": "America/Toronto", "mexico city": "America/Mexico_City",
    "buenos aires": "America/Argentina/Buenos_Aires",
    "sao paulo": "America/Sao_Paulo",
}


def _tz_for_city(city: str) -> ZoneInfo:
    name = CITY_TZ.get((city or "").lower().strip())
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return _DEFAULT_TZ


async def fetch_nowcast(
    city: str,
    lat: float,
    lon: float,
    target_date_iso: str,
    http_client: httpx.AsyncClient,
    ensemble_mean_c: float,
    ensemble_sigma_c: float,
    now_override: datetime | None = None,
) -> dict | None:
    """Compute a nowcast for same-day weather markets.

    Args:
        city: city key (for logging)
        lat, lon: coordinates
        target_date_iso: YYYY-MM-DD of the market's target date (local tz)
        http_client: shared httpx client
        ensemble_mean_c: baseline ensemble forecast max °C
        ensemble_sigma_c: baseline ensemble forecast σ °C
        now_override: testing — pretend "now" is this datetime (tz-aware)

    Returns:
        dict with keys:
            activated (bool): whether nowcast is active
            nowcast_mean_c (float): blended max temperature estimate
            nowcast_sigma_c (float): shrunken σ
            observed_max_so_far (float): max observed temp today
            remaining_hours (int): hours left until midnight local
        None on error or sanity-check failure.
    """
    now_local = (now_override or datetime.now(_tz_for_city(city))).astimezone(
        _tz_for_city(city)
    )
    city_tz = _tz_for_city(city)
    target_local_date = datetime.strptime(target_date_iso, "%Y-%m-%d").date()

    # Only active for target_date == today (local tz).
    if target_local_date != now_local.date():
        return {
            "activated": False,
            "nowcast_mean_c": ensemble_mean_c,
            "nowcast_sigma_c": ensemble_sigma_c,
            "observed_max_so_far": None,
            "remaining_hours": None,
            "reason": "not_today",
        }

    if now_local.hour < NOWCAST_ACTIVE_LOCAL_HOUR:
        return {
            "activated": False,
            "nowcast_mean_c": ensemble_mean_c,
            "nowcast_sigma_c": ensemble_sigma_c,
            "observed_max_so_far": None,
            "remaining_hours": None,
            "reason": "too_early_local_time",
        }

    try:
        resp = await http_client.get(OPEN_METEO_FORECAST, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m",
            "past_days": 1,
            "forecast_days": 1,
            "timezone": str(city_tz),
        })
    except Exception as e:
        log.warning("nowcast_http_error", city=city, err=str(e)[:100])
        return None

    if resp.status_code != 200:
        log.warning("nowcast_bad_status", city=city, status=resp.status_code)
        return None

    try:
        data = resp.json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", []) or []
        temps = hourly.get("temperature_2m", []) or []
    except Exception:
        return None

    if not times or not temps or len(times) != len(temps):
        return None

    observed: list[float] = []
    remaining: list[float] = []
    current_hour = now_local.replace(minute=0, second=0, microsecond=0)
    # Only consider hours on target_date (payload may include yesterday + today).
    target_date = datetime.strptime(target_date_iso, "%Y-%m-%d").date()

    for t_str, val in zip(times, temps):
        if val is None:
            continue
        try:
            # Open-Meteo returns ISO strings in the requested timezone.
            t_local = datetime.fromisoformat(t_str).replace(tzinfo=city_tz)
        except ValueError:
            continue
        if t_local.date() != target_date:
            continue
        if t_local < current_hour:
            observed.append(float(val))
        else:
            remaining.append(float(val))

    if not observed and not remaining:
        return None

    observed_max = max(observed) if observed else ensemble_mean_c
    remaining_max = max(remaining) if remaining else observed_max

    # Sanity: observations can't exceed forecast by more than OBSERVED_ANOMALY_BOUND_C
    # (likely bad station reading). Refuse nowcast rather than blow up sizing.
    if observed and observed_max > ensemble_mean_c + OBSERVED_ANOMALY_BOUND_C:
        log.warning(
            "nowcast_observed_anomaly",
            city=city,
            observed_max=round(observed_max, 1),
            ensemble_mean=round(ensemble_mean_c, 1),
        )
        return None

    nowcast_mean = max(observed_max, remaining_max)
    remaining_hours = len(remaining)
    nowcast_sigma = max(
        MIN_NOWCAST_SIGMA_C,
        ensemble_sigma_c * (remaining_hours / _HOURS_PER_DAY),
    )

    return {
        "activated": True,
        "nowcast_mean_c": nowcast_mean,
        "nowcast_sigma_c": nowcast_sigma,
        "observed_max_so_far": observed_max if observed else None,
        "remaining_hours": remaining_hours,
        "reason": "ok",
    }
