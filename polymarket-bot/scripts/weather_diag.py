#!/usr/bin/env python3
"""Weather strategy diagnostic — analyse why live trades lose.

Hypothesis: bot's Open-Meteo forecast/archive uses CITY-LEVEL grid, while
Polymarket resolves on a specific weather station (airport / observatory).
The grid-vs-station gap can be 1-3°C, which exceeds the bot's typical σ
(~1.0-1.4°C). Result: bot's bucket probabilities are systematically wrong.

This script:
  1. Pulls all closed weather live trades from DB.
  2. For each: parses bot's forecast (mean, σ, bucket/range/threshold) from
     reasoning string + city + target_date from question.
  3. Queries Open-Meteo Archive (city-level grid) for actual max temp.
  4. Compares:
        forecast (what bot predicted)
        archive actual (what bot would have used to resolve in PAPER)
        polymarket outcome (real resolution — inferred from trade.pnl sign)
  5. Highlights cases where archive ≠ polymarket outcome — those expose the
     resolution-source mismatch.

Usage:
    .venv/bin/python scripts/weather_diag.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

import httpx

DB_PATH = "data/trades.db"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

CITY_COORDS = {
    "new york city": (40.71, -74.01),
    "nyc": (40.71, -74.01),
    "los angeles": (34.05, -118.24),
    "london": (51.51, -0.13),
    "tokyo": (35.68, 139.69),
    "hong kong": (22.32, 114.17),
    "shanghai": (31.23, 121.47),
    "seoul": (37.57, 126.98),
    "madrid": (40.42, -3.70),
    "paris": (48.86, 2.35),
    "berlin": (52.52, 13.41),
    "moscow": (55.75, 37.62),
}

# Polymarket resolution stations (best-known mapping)
RESOLUTION_STATIONS = {
    "new york city": "Central Park (KNYC)",
    "nyc": "Central Park (KNYC)",
    "london": "Heathrow (EGLL)",
    "madrid": "Madrid-Barajas (LEMD)",
    "tokyo": "Tokyo (Otemachi)",
    "hong kong": "Hong Kong Observatory",
    "los angeles": "Downtown LA (KCQT)",
    "shanghai": "Shanghai Hongqiao",
    "seoul": "Seoul (RKSI)",
    "paris": "Paris-Montsouris",
    "berlin": "Berlin-Tempelhof",
    "moscow": "Moscow (UUEE)",
}


@dataclass
class Diag:
    trade_id: int
    city: str
    target_date: str
    direction: str
    entry_price: float
    size: float
    pnl: float
    fcst_mean: Optional[float]
    fcst_sigma: Optional[float]
    bucket_low: Optional[float]
    bucket_high: Optional[float]
    bucket_kind: str
    archive_actual: Optional[float] = None


def _parse_reasoning(reasoning: str) -> dict:
    """Extract fcst, σ, bucket bounds from reasoning string.

    Examples:
      'fcst=18.5°C σ=1.35 bucket=18°C [17.5,18.5] | target=2026-04-24'
      'fcst=19.2°C σ=1.28 range=[16.2,17.7]°C | target=2026-04-24'
      'fcst=19.2°C σ=1.28 thr=≥19°C | target=2026-04-24'
      'fcst=21.5°C σ=1.50 thr=≤22°C | target=2026-04-24'
    """
    out: dict = {
        "fcst_mean": None, "fcst_sigma": None,
        "low": None, "high": None, "kind": "?", "target_date": None,
    }
    m = re.search(r"fcst=(-?\d+\.?\d*)°C", reasoning or "")
    if m:
        out["fcst_mean"] = float(m.group(1))
    m = re.search(r"σ=(\d+\.?\d*)", reasoning or "")
    if m:
        out["fcst_sigma"] = float(m.group(1))
    m = re.search(r"target=(\d{4}-\d{2}-\d{2})", reasoning or "")
    if m:
        out["target_date"] = m.group(1)

    # range=[low,high]
    m = re.search(r"range=\[(-?\d+\.?\d*),(-?\d+\.?\d*)\]", reasoning or "")
    if m:
        out["low"] = float(m.group(1))
        out["high"] = float(m.group(2))
        out["kind"] = "range"
        return out
    # bucket=X°C [low,high]
    m = re.search(r"bucket=(-?\d+\.?\d*)°C \[(-?\d+\.?\d*),(-?\d+\.?\d*)\]", reasoning or "")
    if m:
        out["low"] = float(m.group(2))
        out["high"] = float(m.group(3))
        out["kind"] = "bucket"
        return out
    # thr=≥X°C  (at_or_above)
    m = re.search(r"thr=≥(-?\d+\.?\d*)°C", reasoning or "")
    if m:
        out["low"] = float(m.group(1))
        out["high"] = None
        out["kind"] = "at_or_above"
        return out
    # thr=≤X°C  (at_or_below)
    m = re.search(r"thr=≤(-?\d+\.?\d*)°C", reasoning or "")
    if m:
        out["low"] = None
        out["high"] = float(m.group(1))
        out["kind"] = "at_or_below"
    return out


def _parse_city(question: str) -> str:
    """Extract city from question 'WEATHER City | ...'."""
    m = re.search(r"WEATHER ([^|]+?) \|", question or "")
    if m:
        return m.group(1).strip().lower()
    return ""


def _resolve_archive_outcome(actual: float, kind: str,
                             low: Optional[float], high: Optional[float]) -> bool:
    """Did YES win at the archive temperature?"""
    if kind == "at_or_above":
        return low is not None and actual >= low
    if kind == "at_or_below":
        return high is not None and actual <= high
    if kind in ("bucket", "range"):
        if low is None or high is None:
            return False
        return low <= actual <= high
    return False


async def _fetch_archive(client, lat: float, lon: float, date: str) -> Optional[float]:
    try:
        r = await client.get(OPEN_METEO_ARCHIVE, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "timezone": "America/New_York",
            "start_date": date, "end_date": date,
        })
        if r.status_code != 200:
            return None
        vals = r.json().get("daily", {}).get("temperature_2m_max", [])
        return float(vals[0]) if vals and vals[0] is not None else None
    except Exception:
        return None


async def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, question, direction, price, size, pnl, reasoning, created_at
        FROM trades
        WHERE paper_mode = 0
          AND status = 'closed'
          AND question LIKE 'WEATHER%'
        ORDER BY id
    """)
    rows = cur.fetchall()
    if not rows:
        print("No closed live weather trades to diagnose.")
        return 0

    diags: list[Diag] = []
    for tid, question, direction, price, size, pnl, reasoning, _ in rows:
        city = _parse_city(question)
        parsed = _parse_reasoning(reasoning or "")
        diags.append(Diag(
            trade_id=tid, city=city,
            target_date=parsed["target_date"] or "?",
            direction=direction or "",
            entry_price=float(price or 0),
            size=float(size or 0),
            pnl=float(pnl or 0),
            fcst_mean=parsed["fcst_mean"],
            fcst_sigma=parsed["fcst_sigma"],
            bucket_low=parsed["low"],
            bucket_high=parsed["high"],
            bucket_kind=parsed["kind"],
        ))

    async with httpx.AsyncClient(timeout=20) as client:
        for d in diags:
            coords = CITY_COORDS.get(d.city)
            if not coords or d.target_date == "?":
                continue
            d.archive_actual = await _fetch_archive(client, coords[0], coords[1], d.target_date)

    print()
    print("=" * 110)
    print(f"{'ID':>4} {'City':<14} {'Date':<12} {'Side':<8} {'Fcst':>6} {'σ':>5} "
          f"{'Bucket':<22} {'Archive':>8} {'Resid':>7} {'PaperWin':<8} {'Real':<5}")
    print("=" * 110)
    sum_resid = 0.0
    sum_resid_count = 0
    paper_disagrees_with_real = 0

    for d in diags:
        bucket_str = "?"
        if d.bucket_kind in ("bucket", "range") and d.bucket_low is not None and d.bucket_high is not None:
            bucket_str = f"[{d.bucket_low:.1f}, {d.bucket_high:.1f}]"
        elif d.bucket_kind == "at_or_above" and d.bucket_low is not None:
            bucket_str = f"≥{d.bucket_low:.1f}"
        elif d.bucket_kind == "at_or_below" and d.bucket_high is not None:
            bucket_str = f"≤{d.bucket_high:.1f}"

        side = "YES" if "BUY_YES" in (d.direction or "") else "NO"
        is_buy_yes = (side == "YES")

        # Did archive say YES wins? (= what paper would resolve)
        if d.archive_actual is not None and d.bucket_kind != "?":
            yes_won_per_archive = _resolve_archive_outcome(
                d.archive_actual, d.bucket_kind, d.bucket_low, d.bucket_high
            )
            paper_outcome = "WIN" if (is_buy_yes == yes_won_per_archive) else "LOSS"
        else:
            paper_outcome = "?"

        real_outcome = "WIN" if d.pnl > 0 else "LOSS"

        if paper_outcome != "?" and paper_outcome != real_outcome:
            paper_disagrees_with_real += 1

        # Residual = archive_actual - forecast_mean
        residual_str = "—"
        if d.archive_actual is not None and d.fcst_mean is not None:
            res = d.archive_actual - d.fcst_mean
            residual_str = f"{res:+.2f}"
            sum_resid += res
            sum_resid_count += 1

        archive_str = f"{d.archive_actual:.2f}" if d.archive_actual is not None else "—"
        fcst_str = f"{d.fcst_mean:.1f}" if d.fcst_mean is not None else "—"
        sigma_str = f"{d.fcst_sigma:.2f}" if d.fcst_sigma is not None else "—"

        print(f"{d.trade_id:>4} {d.city:<14} {d.target_date:<12} {side:<8} "
              f"{fcst_str:>6} {sigma_str:>5} {bucket_str:<22} "
              f"{archive_str:>8} {residual_str:>7} {paper_outcome:<8} {real_outcome:<5}")

    print("=" * 110)
    print()
    print("--- Summary ---")
    if sum_resid_count > 0:
        mean_resid = sum_resid / sum_resid_count
        print(f"Mean residual (archive_actual - forecast):  {mean_resid:+.2f}°C  "
              f"(n={sum_resid_count})")
        if abs(mean_resid) > 0.5:
            print(f"  → Forecast vs Open-Meteo archive show systematic offset.")
        else:
            print(f"  → Forecast aligns well with archive (residual small).")
    print(f"Cases where ARCHIVE outcome ≠ POLYMARKET outcome: {paper_disagrees_with_real}/{len(diags)}")
    if paper_disagrees_with_real >= len(diags) // 2:
        print(f"  → PAPER would have RESOLVED DIFFERENTLY from real Polymarket.")
        print(f"     This is the resolution-source mismatch: bot uses city-grid,")
        print(f"     Polymarket uses specific weather station.")
    print()
    print("Polymarket resolution stations (city-grid is NOT used):")
    for d in diags[:5]:
        st = RESOLUTION_STATIONS.get(d.city, "unknown")
        print(f"  {d.city:<14} → {st}")
    print()
    print("Recommendations based on findings:")
    print("  1. If residual large + outcomes mismatch: switch resolution source")
    print("     to actual station data (e.g. NWS API for US, ECMWF MARS for EU).")
    print("  2. If σ too narrow: widen σ by sqrt(2) or recalibrate per-city.")
    print("  3. Pause weather strategy until forecast aligned with Polymarket source.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
