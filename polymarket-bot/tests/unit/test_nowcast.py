"""Unit tests for bot/nowcast.py — same-day weather nowcasting."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from bot.nowcast import (
    fetch_nowcast,
    NOWCAST_ACTIVE_LOCAL_HOUR,
    OBSERVED_ANOMALY_BOUND_C,
    MIN_NOWCAST_SIGMA_C,
)


ET = ZoneInfo("America/New_York")


def _make_http_response(times: list[str], temps: list[float | None], status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value={"hourly": {"time": times, "temperature_2m": temps}})
    return resp


def _make_client(response) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    return client


def _make_hourly(target_date: str, temps: list[float]) -> tuple[list[str], list[float]]:
    """Build 24 ISO hourly timestamps for target_date and temps."""
    times = [f"{target_date}T{h:02d}:00" for h in range(len(temps))]
    return times, temps


class TestFetchNowcast:
    @pytest.mark.asyncio
    async def test_before_14_local_returns_baseline(self):
        # 10 AM — too early.
        now = datetime(2026, 4, 21, 10, 0, tzinfo=ET)
        times, temps = _make_hourly("2026-04-21", [12.0] * 24)
        client = _make_client(_make_http_response(times, temps))
        result = await fetch_nowcast(
            city="nyc", lat=40.71, lon=-74.01,
            target_date_iso="2026-04-21",
            http_client=client,
            ensemble_mean_c=15.0, ensemble_sigma_c=1.5,
            now_override=now,
        )
        assert result is not None
        assert result["activated"] is False
        assert result["nowcast_mean_c"] == 15.0
        assert result["nowcast_sigma_c"] == 1.5
        assert result["reason"] == "too_early_local_time"
        # Never hits HTTP because early exit
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_date_not_today_returns_baseline(self):
        now = datetime(2026, 4, 21, 18, 0, tzinfo=ET)
        times, temps = _make_hourly("2026-04-22", [10.0] * 24)
        client = _make_client(_make_http_response(times, temps))
        result = await fetch_nowcast(
            city="nyc", lat=40.71, lon=-74.01,
            target_date_iso="2026-04-22",  # tomorrow
            http_client=client,
            ensemble_mean_c=15.0, ensemble_sigma_c=1.5,
            now_override=now,
        )
        assert result is not None
        assert result["activated"] is False
        assert result["reason"] == "not_today"
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_observed_below_forecast_returns_forecast_max(self):
        # 17:00 local; past hours peak at 14°C, remaining forecast peaks 15°C
        now = datetime(2026, 4, 21, 17, 0, tzinfo=ET)
        temps = [5, 5, 6, 7, 8, 9, 10, 11, 11, 12, 12, 13, 13, 14, 14,
                 14, 14, 15, 15, 14, 13, 12, 11, 10]
        times, temps = _make_hourly("2026-04-21", temps)
        client = _make_client(_make_http_response(times, temps))
        result = await fetch_nowcast(
            city="nyc", lat=40.71, lon=-74.01,
            target_date_iso="2026-04-21",
            http_client=client,
            ensemble_mean_c=15.0, ensemble_sigma_c=1.5,
            now_override=now,
        )
        assert result is not None
        assert result["activated"] is True
        # observed_max up to hour 16 = 14; remaining 17-23 peaks at 15
        assert result["observed_max_so_far"] == 14.0
        assert result["nowcast_mean_c"] == 15.0  # remaining max beats observed
        # remaining hours = 24-17 = 7
        assert result["remaining_hours"] == 7
        assert result["nowcast_sigma_c"] == pytest.approx(1.5 * 7 / 24, rel=1e-3)

    @pytest.mark.asyncio
    async def test_observed_upgrades_mean_when_above_forecast(self):
        # 18:00: observed already at 16°C, inside anomaly bound (15+2).
        now = datetime(2026, 4, 21, 18, 0, tzinfo=ET)
        temps = [5] * 10 + [10, 11, 12, 13, 14, 15, 16, 16] + [15, 14, 13, 12, 11, 10]
        times, temps = _make_hourly("2026-04-21", temps)
        client = _make_client(_make_http_response(times, temps))
        result = await fetch_nowcast(
            city="nyc", lat=40.71, lon=-74.01,
            target_date_iso="2026-04-21",
            http_client=client,
            ensemble_mean_c=15.0, ensemble_sigma_c=1.5,
            now_override=now,
        )
        assert result is not None
        assert result["activated"] is True
        assert result["observed_max_so_far"] == 16.0
        # remaining (hours 18-23) peaks at 15 → overall nowcast = max(16,15) = 16
        assert result["nowcast_mean_c"] == 16.0

    @pytest.mark.asyncio
    async def test_observed_anomaly_returns_none(self):
        # observed 22°C while ensemble says 15 → 7°C off, clearly bad data
        now = datetime(2026, 4, 21, 16, 0, tzinfo=ET)
        temps = [5] * 10 + [10, 12, 15, 18, 20, 22] + [21, 20, 19, 17, 15, 13, 11, 10]
        times, temps = _make_hourly("2026-04-21", temps)
        client = _make_client(_make_http_response(times, temps))
        result = await fetch_nowcast(
            city="nyc", lat=40.71, lon=-74.01,
            target_date_iso="2026-04-21",
            http_client=client,
            ensemble_mean_c=15.0, ensemble_sigma_c=1.5,
            now_override=now,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_sigma_shrinks_with_remaining_hours(self):
        # 20:00 — only 4 remaining hours
        now = datetime(2026, 4, 21, 20, 0, tzinfo=ET)
        temps = [10] * 24
        times, temps = _make_hourly("2026-04-21", temps)
        client = _make_client(_make_http_response(times, temps))
        result = await fetch_nowcast(
            city="nyc", lat=40.71, lon=-74.01,
            target_date_iso="2026-04-21",
            http_client=client,
            ensemble_mean_c=10.0, ensemble_sigma_c=2.0,
            now_override=now,
        )
        assert result is not None
        assert result["activated"] is True
        assert result["remaining_hours"] == 4
        assert result["nowcast_sigma_c"] == pytest.approx(2.0 * 4 / 24, rel=1e-3)

    @pytest.mark.asyncio
    async def test_end_of_day_uses_sigma_floor(self):
        # 23:59 local — only 1 hour (23:00) in remaining; σ floor kicks in
        # since 1.5 * 1/24 = 0.0625 < MIN_NOWCAST_SIGMA_C.
        now = datetime(2026, 4, 21, 23, 59, tzinfo=ET)
        temps = [10] * 24
        times, temps = _make_hourly("2026-04-21", temps)
        client = _make_client(_make_http_response(times, temps))
        result = await fetch_nowcast(
            city="nyc", lat=40.71, lon=-74.01,
            target_date_iso="2026-04-21",
            http_client=client,
            ensemble_mean_c=10.0, ensemble_sigma_c=1.5,
            now_override=now,
        )
        assert result is not None
        assert result["activated"] is True
        assert result["remaining_hours"] == 1
        assert result["nowcast_sigma_c"] == MIN_NOWCAST_SIGMA_C

    @pytest.mark.asyncio
    async def test_http_error_returns_none(self):
        now = datetime(2026, 4, 21, 16, 0, tzinfo=ET)
        client = AsyncMock()
        client.get = AsyncMock(side_effect=RuntimeError("boom"))
        result = await fetch_nowcast(
            city="nyc", lat=40.71, lon=-74.01,
            target_date_iso="2026-04-21",
            http_client=client,
            ensemble_mean_c=15.0, ensemble_sigma_c=1.5,
            now_override=now,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_bad_status_returns_none(self):
        now = datetime(2026, 4, 21, 16, 0, tzinfo=ET)
        client = _make_client(_make_http_response([], [], status=500))
        result = await fetch_nowcast(
            city="nyc", lat=40.71, lon=-74.01,
            target_date_iso="2026-04-21",
            http_client=client,
            ensemble_mean_c=15.0, ensemble_sigma_c=1.5,
            now_override=now,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_hourly_returns_none(self):
        now = datetime(2026, 4, 21, 16, 0, tzinfo=ET)
        client = _make_client(_make_http_response([], []))
        result = await fetch_nowcast(
            city="nyc", lat=40.71, lon=-74.01,
            target_date_iso="2026-04-21",
            http_client=client,
            ensemble_mean_c=15.0, ensemble_sigma_c=1.5,
            now_override=now,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_malformed_lengths_returns_none(self):
        # times and temps lengths mismatch → bail out
        now = datetime(2026, 4, 21, 16, 0, tzinfo=ET)
        times, temps = _make_hourly("2026-04-21", [10.0] * 24)
        client = _make_client(_make_http_response(times[:10], temps))
        result = await fetch_nowcast(
            city="nyc", lat=40.71, lon=-74.01,
            target_date_iso="2026-04-21",
            http_client=client,
            ensemble_mean_c=15.0, ensemble_sigma_c=1.5,
            now_override=now,
        )
        assert result is None
