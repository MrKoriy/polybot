#!/usr/bin/env python3
"""Backtest weather strategy with station-based resolution.

Simulates bot's decision flow on historical data using:
  - Forecast: Open-Meteo daily max forecast (queried from archive — same data
    that was available at trade-time, ±0.5°C noise vs real-time forecast)
  - Resolution: METAR station observation (matches Polymarket)

For each city × past N days:
  1. Get forecast (from Open-Meteo archive at city center)
  2. Get actual (from METAR at Polymarket's station)
  3. Compute residual = actual - forecast
  4. Report: per-city bias, σ, prediction skill

If residual is large/biased → forecast model needs calibration before re-enable.
If residual is small → station fix is sufficient, re-enable safely.

Usage:
    .venv/bin/python scripts/weather_backtest.py [--days 14] [--cities nyc,london]
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.station_weather import STATIONS, get_station_max_c

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

# City center coords (what current weather_trader uses for forecast)
CITY_CENTER = {
    "new york city":  (40.71, -74.01),
    "london":         (51.51, -0.13),
    "miami":          (25.76, -80.19),
    "paris":          (48.86,   2.35),
    "berlin":         (52.52,  13.41),
    "madrid":         (40.42,  -3.70),
    "tokyo":          (35.68, 139.69),
    "seoul":          (37.57, 126.98),
    "shanghai":       (31.23, 121.47),
    "hong kong":      (22.32, 114.17),
    "moscow":         (55.75,  37.62),
    "sydney":         (-33.87,151.21),
}


async def get_archive_max(client, lat, lon, date) -> float | None:
    """Query Open-Meteo archive for daily max temperature at lat/lon."""
    try:
        r = await client.get(OPEN_METEO_ARCHIVE, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "timezone": "auto",
            "start_date": date, "end_date": date,
        })
        if r.status_code != 200:
            return None
        vals = r.json().get("daily", {}).get("temperature_2m_max", [])
        return float(vals[0]) if vals and vals[0] is not None else None
    except Exception:
        return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--cities", default="all",
                    help="comma-separated city list, or 'all'")
    ap.add_argument("--coords", choices=["city", "airport"], default="city",
                    help="Use city-center or airport coords for Open-Meteo forecast")
    args = ap.parse_args()

    cities = list(CITY_CENTER.keys()) if args.cities == "all" \
        else [c.strip().lower() for c in args.cities.split(",")]

    today = datetime.utcnow().date()
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(2, args.days + 2)]  # skip today/yesterday (archive lag)

    print(f"Backtesting {len(cities)} cities × {len(dates)} days = "
          f"{len(cities) * len(dates)} samples")
    print()
    print(f"{'Date':<12} {'City':<14} {'Open-Meteo':>11} {'Station':>9} {'Resid':>7}")
    print("=" * 60)

    by_city: dict[str, list[float]] = defaultdict(list)
    overall: list[float] = []

    async with httpx.AsyncClient(timeout=20) as client:
        for city in cities:
            if city not in CITY_CENTER or city not in STATIONS:
                continue
            if args.coords == "airport":
                _, _, lat, lon = STATIONS[city]
            else:
                lat, lon = CITY_CENTER[city]
            for date in dates:
                grid = await get_archive_max(client, lat, lon, date)
                station = await get_station_max_c(client, city, date)
                if grid is None or station is None:
                    continue
                residual = station - grid
                by_city[city].append(residual)
                overall.append(residual)
                print(f"{date:<12} {city:<14} {grid:>10.1f}°C  {station:>7.1f}°C "
                      f"{residual:>+6.2f}")

    print("=" * 60)
    print()
    print("--- Per-city bias (station - grid) ---")
    print(f"{'City':<14} {'n':>3} {'mean':>7} {'σ':>6} {'min':>7} {'max':>7}")
    print("-" * 55)
    for city, residuals in sorted(by_city.items()):
        if not residuals:
            continue
        n = len(residuals)
        mean = statistics.mean(residuals)
        sd = statistics.stdev(residuals) if n > 1 else 0.0
        print(f"{city:<14} {n:>3d} {mean:>+6.2f}°C {sd:>5.2f}°C "
              f"{min(residuals):>+6.2f} {max(residuals):>+6.2f}")
    print()
    if overall:
        print("--- Overall ---")
        print(f"Samples: {len(overall)}")
        print(f"Mean residual:  {statistics.mean(overall):+.2f}°C")
        if len(overall) > 1:
            print(f"Stdev residual: {statistics.stdev(overall):.2f}°C")
        print(f"Range:          {min(overall):+.2f} to {max(overall):+.2f}°C")
    print()
    print("Interpretation:")
    print("  - If |mean| > 1°C: forecast (city-grid) systematically off vs station")
    print("    → calibrate by adjusting forecast mean per city")
    print("  - If σ > 2°C: forecast unreliable for narrow buckets")
    print("    → widen σ in weather_trader._SIGMA_BY_LEAD_DAYS")
    print("  - If both small: forecast is OK, just resolution was wrong (already fixed)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
