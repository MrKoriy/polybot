"""Unit tests for bot/weather_anomaly_filter.py."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bot.weather_anomaly_filter import WeatherAnomalyFilter


def _make_llm(response):
    llm = AsyncMock()
    llm.call_json = AsyncMock(return_value=response)
    return llm


class TestWeatherAnomalyFilter:
    @pytest.mark.asyncio
    async def test_no_llm_returns_fail_open(self):
        f = WeatherAnomalyFilter(llm_client=None)
        result = await f.check("nyc", "2026-04-22", 15.0, 1.5)
        assert result["anomaly"] is False
        assert result["source"] == "fallback"
        assert f.should_skip(result) is False

    @pytest.mark.asyncio
    async def test_anomaly_detected_high_confidence(self):
        llm = _make_llm({
            "anomaly": True, "event": "polar vortex",
            "confidence": 0.85, "reason": "forecast of -15C below ensemble",
        })
        f = WeatherAnomalyFilter(llm, min_confidence=0.6)
        result = await f.check("nyc", "2026-04-22", 5.0, 1.5)
        assert result["anomaly"] is True
        assert result["event"] == "polar vortex"
        assert result["confidence"] == 0.85
        assert result["source"] == "llm"
        assert f.should_skip(result) is True
        llm.call_json.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_anomaly_low_confidence_does_not_skip(self):
        llm = _make_llm({
            "anomaly": True, "event": "maybe storm",
            "confidence": 0.4, "reason": "weak signal",
        })
        f = WeatherAnomalyFilter(llm, min_confidence=0.6)
        result = await f.check("la", "2026-04-22", 25.0, 1.0)
        assert result["anomaly"] is True
        assert result["confidence"] == 0.4
        assert f.should_skip(result) is False  # below threshold

    @pytest.mark.asyncio
    async def test_cache_hit_no_second_llm_call(self):
        llm = _make_llm({
            "anomaly": False, "event": "", "confidence": 0.2, "reason": "quiet",
        })
        f = WeatherAnomalyFilter(llm)
        r1 = await f.check("nyc", "2026-04-22", 15.0, 1.5)
        r2 = await f.check("nyc", "2026-04-22", 15.0, 1.5)
        assert r1["source"] == "llm"
        assert r2["source"] == "cache"
        assert llm.call_json.await_count == 1

    @pytest.mark.asyncio
    async def test_cache_different_city_calls_llm_again(self):
        llm = _make_llm({
            "anomaly": False, "event": "", "confidence": 0.1, "reason": "",
        })
        f = WeatherAnomalyFilter(llm)
        await f.check("nyc", "2026-04-22", 15.0, 1.5)
        await f.check("la", "2026-04-22", 25.0, 1.0)
        assert llm.call_json.await_count == 2

    @pytest.mark.asyncio
    async def test_llm_returns_none_fail_open(self):
        llm = _make_llm(None)
        f = WeatherAnomalyFilter(llm)
        result = await f.check("nyc", "2026-04-22", 15.0, 1.5)
        assert result["anomaly"] is False
        assert result["source"] == "fallback"
        assert f.should_skip(result) is False

    @pytest.mark.asyncio
    async def test_llm_returns_malformed_fail_open(self):
        llm = _make_llm("not a dict")
        f = WeatherAnomalyFilter(llm)
        result = await f.check("nyc", "2026-04-22", 15.0, 1.5)
        assert result["anomaly"] is False
        assert result["source"] == "fallback"

    @pytest.mark.asyncio
    async def test_confidence_clamped(self):
        llm = _make_llm({
            "anomaly": True, "event": "x", "confidence": 1.7, "reason": "",
        })
        f = WeatherAnomalyFilter(llm)
        result = await f.check("nyc", "2026-04-22", 15.0, 1.5)
        assert result["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_cache_expires(self):
        llm = _make_llm({
            "anomaly": False, "event": "", "confidence": 0.1, "reason": "",
        })
        f = WeatherAnomalyFilter(llm, cache_ttl_seconds=0)
        await f.check("nyc", "2026-04-22", 15.0, 1.5)
        await f.check("nyc", "2026-04-22", 15.0, 1.5)
        assert llm.call_json.await_count == 2
