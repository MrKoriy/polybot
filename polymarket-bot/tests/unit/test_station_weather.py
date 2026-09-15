"""Unit tests for bot/station_weather.py."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from bot.station_weather import (
    STATIONS,
    get_station_for_city,
    get_station_max_c,
)


def _mock_metar_response(observations, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=observations)
    return resp


def _make_obs(ts_utc: datetime, temp_c: float) -> dict:
    return {"obsTime": int(ts_utc.timestamp()), "temp": temp_c}


@pytest.fixture
def client():
    c = AsyncMock()
    return c


class TestStationLookup:
    def test_known_city(self):
        info = get_station_for_city("new york city")
        assert info is not None
        assert info[0] == "KLGA"

    def test_alias(self):
        info = get_station_for_city("nyc")
        assert info is not None
        assert info[0] == "KLGA"

    def test_unknown(self):
        assert get_station_for_city("atlantis") is None

    def test_case_insensitive(self):
        assert get_station_for_city("NEW YORK CITY") is not None
        assert get_station_for_city("  London  ") is not None


class TestGetStationMaxC:
    @pytest.mark.asyncio
    async def test_unknown_city_returns_none(self, client):
        result = await get_station_max_c(client, "atlantis", "2026-04-24")
        assert result is None
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_filters_to_local_day(self, client):
        # NYC LaGuardia, target April 24 ET
        # Day spans April 24 04:00 UTC → April 25 04:00 UTC
        ET = ZoneInfo("America/New_York")
        in_day = [
            _make_obs(datetime(2026, 4, 24, 8, 0, tzinfo=timezone.utc), 10.0),
            _make_obs(datetime(2026, 4, 24, 18, 0, tzinfo=timezone.utc), 18.3),
            _make_obs(datetime(2026, 4, 24, 22, 0, tzinfo=timezone.utc), 16.0),
        ]
        outside = [
            _make_obs(datetime(2026, 4, 23, 22, 0, tzinfo=timezone.utc), 25.0),  # Apr 23 ET
            _make_obs(datetime(2026, 4, 25, 6, 0, tzinfo=timezone.utc), 30.0),   # Apr 25 ET
        ]
        client.get = AsyncMock(return_value=_mock_metar_response(in_day + outside))
        result = await get_station_max_c(client, "new york city", "2026-04-24")
        assert result == 18.3

    @pytest.mark.asyncio
    async def test_empty_response_returns_none(self, client):
        client.get = AsyncMock(return_value=_mock_metar_response([]))
        result = await get_station_max_c(client, "new york city", "2026-04-24")
        assert result is None

    @pytest.mark.asyncio
    async def test_bad_status_returns_none(self, client):
        client.get = AsyncMock(return_value=_mock_metar_response([], status=500))
        result = await get_station_max_c(client, "new york city", "2026-04-24")
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_obs_with_missing_temp(self, client):
        obs = [
            _make_obs(datetime(2026, 4, 24, 18, 0, tzinfo=timezone.utc), 15.0),
            {"obsTime": int(datetime(2026, 4, 24, 19, 0, tzinfo=timezone.utc).timestamp()),
             "temp": None},
            _make_obs(datetime(2026, 4, 24, 20, 0, tzinfo=timezone.utc), 12.0),
        ]
        client.get = AsyncMock(return_value=_mock_metar_response(obs))
        result = await get_station_max_c(client, "new york city", "2026-04-24")
        assert result == 15.0

    @pytest.mark.asyncio
    async def test_invalid_date_returns_none(self, client):
        result = await get_station_max_c(client, "new york city", "not-a-date")
        assert result is None

    @pytest.mark.asyncio
    async def test_uses_correct_icao(self, client):
        client.get = AsyncMock(return_value=_mock_metar_response([]))
        await get_station_max_c(client, "london", "2026-04-24")
        call_args = client.get.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params")
        assert params["ids"] == "EGLC"

    @pytest.mark.asyncio
    async def test_query_window_covers_local_day(self, client):
        """Query date param should be END of local day + buffer (UTC)."""
        client.get = AsyncMock(return_value=_mock_metar_response([]))
        await get_station_max_c(client, "new york city", "2026-04-24")
        params = client.get.call_args.kwargs.get("params") or client.get.call_args[1].get("params")
        # End of April 24 ET = April 25 04:00 UTC; +4h buffer = April 25 08:00 UTC
        assert params["date"] == "20260425_0800"
        assert params["hours"] == 36


class TestStationsCoverage:
    """Sanity: STATIONS dict covers all cities the weather_trader knows."""

    def test_major_polymarket_cities(self):
        required = [
            "new york city", "london", "tokyo", "seoul", "miami",
            "paris", "berlin", "madrid", "moscow", "hong kong",
            "shanghai", "singapore", "sydney",
        ]
        for city in required:
            assert get_station_for_city(city) is not None, f"missing station for {city!r}"

    def test_each_entry_has_4_fields(self):
        for city, info in STATIONS.items():
            assert len(info) == 4, f"{city}: expected 4-tuple, got {len(info)}"
            icao, tz, lat, lon = info
            assert isinstance(icao, str) and len(icao) == 4
            assert isinstance(tz, str) and "/" in tz
            assert -90 <= lat <= 90
            assert -180 <= lon <= 180
