"""LLM-based anomaly filter for weather markets.

Runs a cheap Gemini/Claude check once per (city, target_date) to ask whether
any known extreme weather event (tornado outbreak, heat dome, polar vortex,
hurricane, atmospheric river, derecho) in the next 48h is NOT already
captured by a standard 3-model meteorological ensemble.

If anomaly.confidence ≥ threshold → trade skipped by weather_trader.

Design:
- Cache 6h per (city, date) → ~4 calls/day × 26 cities = ~100/day. Cost: cents.
- Fail-open: any error/timeout/bad JSON → returns `anomaly=False`, trade proceeds.
- Model reuse: LLMClient.call_json (primary provider, typically openrouter).
"""
from __future__ import annotations

import time as _time

import structlog

from analysis.llm_client import LLMClient

log = structlog.get_logger()

DEFAULT_CACHE_TTL_S = 6 * 3600  # 6 hours

_SYSTEM_PROMPT = (
    "You are a meteorology expert evaluating weather prediction markets. "
    "You know the limits of standard ensemble forecasts (GFS, ECMWF, ICON). "
    "Answer strictly with JSON, no commentary."
)

_USER_TEMPLATE = (
    "City: {city}\n"
    "Target date: {target_date}\n"
    "Ensemble-forecast max temperature: {mean:.1f}°C (σ={sigma:.1f}°C)\n\n"
    "Question: Within the next 48 hours for this city, is there a KNOWN extreme "
    "weather event (tornado outbreak, heat dome, polar vortex, hurricane landfall, "
    "atmospheric river, derecho, major cold snap) that is under-represented by a "
    "standard 3-model meteorological ensemble? Only flag events that could shift "
    "the daily max temperature by > 3°C from the ensemble mean.\n\n"
    "Reply JSON ONLY:\n"
    '{{"anomaly": true|false, "event": "<name or empty>", '
    '"confidence": 0.0-1.0, "reason": "<one short sentence>"}}'
)

_FAIL_OPEN = {
    "anomaly": False,
    "event": "",
    "confidence": 0.0,
    "reason": "",
    "source": "fallback",
}


class WeatherAnomalyFilter:
    """Per-(city,date) cached LLM anomaly check for weather markets."""

    def __init__(
        self,
        llm_client: LLMClient | None,
        cache_ttl_seconds: int = DEFAULT_CACHE_TTL_S,
        min_confidence: float = 0.6,
    ) -> None:
        self._llm = llm_client
        self._ttl = cache_ttl_seconds
        self._min_conf = min_confidence
        # key = (city, target_date_iso) -> (payload_dict, monotonic_ts)
        self._cache: dict[tuple[str, str], tuple[dict, float]] = {}

    @property
    def min_confidence(self) -> float:
        return self._min_conf

    async def check(
        self,
        city: str,
        target_date: str,
        ensemble_mean_c: float,
        ensemble_sigma_c: float,
    ) -> dict:
        """Returns anomaly dict with keys: anomaly, event, confidence, reason, source."""
        if self._llm is None:
            return dict(_FAIL_OPEN)

        key = (city.lower(), target_date)
        now = _time.monotonic()
        cached = self._cache.get(key)
        if cached and (now - cached[1]) < self._ttl:
            payload = dict(cached[0])
            payload["source"] = "cache"
            return payload

        user_prompt = _USER_TEMPLATE.format(
            city=city, target_date=target_date,
            mean=ensemble_mean_c, sigma=ensemble_sigma_c,
        )
        raw = await self._llm.call_json(_SYSTEM_PROMPT, user_prompt, max_tokens=256, timeout_s=10.0)
        if not isinstance(raw, dict):
            log.debug("anomaly_filter_fail_open", city=city)
            return dict(_FAIL_OPEN)

        payload = {
            "anomaly": bool(raw.get("anomaly", False)),
            "event": str(raw.get("event") or "")[:64],
            "confidence": max(0.0, min(1.0, float(raw.get("confidence") or 0.0))),
            "reason": str(raw.get("reason") or "")[:200],
            "source": "llm",
        }
        self._cache[key] = (dict(payload), now)
        if payload["anomaly"]:
            log.info(
                "weather_anomaly_detected",
                city=city, date=target_date,
                anomaly_event=payload["event"],
                confidence=payload["confidence"],
                reason=payload["reason"],
            )
        return payload

    def should_skip(self, anomaly: dict) -> bool:
        """Convenience: does this anomaly result warrant skipping the trade?"""
        return bool(anomaly.get("anomaly")) and float(anomaly.get("confidence", 0)) >= self._min_conf
