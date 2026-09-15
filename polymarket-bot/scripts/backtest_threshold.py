"""Calibration backtest for the threshold BS model against ACTUAL outcomes.

The original "14/14 WINS, 100% WR" validation was fabricated: paper trades
resolved via the bot's own Binance oracle, not the market. This script does
the honest version:

  1. Fetch RESOLVED crypto threshold markets from Gamma (real outcomes).
  2. Reconstruct the model's probability at T-24h before expiry using
     historical Binance klines (spot + realized vol at that moment).
  3. Report:
     - calibration: model P_yes bucket vs actual YES frequency
     - directional accuracy and Brier score
     - ORACLE DISAGREEMENT RATE: how often Binance-at-expiry disagrees with
       the market's actual resolution — quantifies exactly how much the old
       paper oracle lied.

Usage: python scripts/backtest_threshold.py [--hours-before 24] [--pages 10]
"""

import argparse
import asyncio
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.threshold_trader import (  # noqa: E402
    _parse_threshold_question,
    prob_above,
)

GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_KLINES = "https://data-api.binance.vision/api/v3/klines"

PAGES = 10
PER_PAGE = 100
HOURS_BEFORE = 24
VOL_LOOKBACK_H = 48

# Daily "above ___" series: event slug = "{asset}-above-on-{month}-{day}-{year}"
SERIES_ASSETS = ("bitcoin", "ethereum", "solana", "xrp")
_SERIES_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


def _series_slug(asset: str, d: datetime) -> str:
    return f"{asset}-above-on-{_SERIES_MONTHS[d.month - 1]}-{d.day}-{d.year}"


async def fetch_series_events(http: httpx.AsyncClient, days_back: int) -> list[dict]:
    """Enumerate daily threshold events by slug — far more complete than the
    volume scan (Gamma ignores tag/search filters, verified Sep 2026)."""
    markets: list[dict] = []
    seen: set[str] = set()
    today = datetime.now(timezone.utc).date()
    slugs = []
    for d_off in range(0, days_back):
        d = today - timedelta(days=d_off)
        for asset in SERIES_ASSETS:
            slugs.append(_series_slug(asset, d))

    async def _fetch(slug: str):
        try:
            resp = await http.get(f"{GAMMA_API}/events", params={"slug": slug})
            if resp.status_code != 200:
                return []
            return resp.json()
        except Exception:
            return []

    sem = asyncio.Semaphore(8)

    async def _one(slug: str):
        async with sem:
            return await _fetch(slug)

    results = await asyncio.gather(*[_one(s) for s in slugs])
    for evs in results:
        for ev in evs:
            for m in ev.get("markets", []) or []:
                cid = m.get("conditionId", "")
                if cid and cid not in seen:
                    seen.add(cid)
                    markets.append(m)
    return markets


async def fetch_resolved_markets(
    http: httpx.AsyncClient, pages: int, series_days: int = 0
) -> list[dict]:
    """Two sources: volume-ordered /markets scan + per-asset-tag /events scan."""
    markets: list[dict] = []
    seen: set[str] = set()

    def add(ms: list[dict]) -> None:
        for m in ms:
            cid = m.get("conditionId", "")
            if cid and cid not in seen:
                seen.add(cid)
                markets.append(m)

    for offset in range(0, pages * PER_PAGE, PER_PAGE):
        try:
            resp = await http.get(
                f"{GAMMA_API}/markets",
                params={
                    "closed": "true",
                    "limit": PER_PAGE,
                    "offset": offset,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            add(batch)
        except Exception as e:
            print(f"gamma fetch error at offset {offset}: {e}")
            break

    # Tag scan: crypto price events, closed (mostly ineffective — Gamma
    # ignores tag filters — but cheap)
    for tag in ("bitcoin", "ethereum", "solana", "xrp", "dogecoin", "crypto"):
        for offset in range(0, pages * 50, 50):
            try:
                resp = await http.get(
                    f"{GAMMA_API}/events",
                    params={"closed": "true", "limit": 50, "offset": offset, "tag": tag},
                )
                if resp.status_code != 200:
                    break
                events = resp.json()
                if not events:
                    break
                for ev in events:
                    add(ev.get("markets", []) or [])
            except Exception:
                break

    if series_days > 0:
        add(await fetch_series_events(http, series_days))

    return markets


def parse_outcome(m: dict) -> bool | None:
    """Actual market outcome: True = YES won (outcome index 0)."""
    raw = m.get("outcomePrices", "")
    prices = json.loads(raw) if isinstance(raw, str) else (raw or [])
    if not prices:
        return None
    try:
        p0 = float(prices[0])
    except (TypeError, ValueError):
        return None
    if p0 == 1.0:
        return True
    if p0 == 0.0:
        return False
    return None


async def fetch_klines(
    http: httpx.AsyncClient, symbol: str, start_ms: int, end_ms: int
) -> list[float]:
    try:
        resp = await http.get(
            BINANCE_KLINES,
            params={
                "symbol": symbol,
                "interval": "1h",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 200,
            },
        )
        if resp.status_code != 200:
            return []
        return [float(k[4]) for k in resp.json()]
    except Exception:
        return []


def realized_daily_sigma(closes: list[float]) -> float | None:
    if len(closes) < 12:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 6:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return math.sqrt(var) * math.sqrt(24.0)


def parse_end_date(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours-before", type=int, default=HOURS_BEFORE)
    parser.add_argument("--pages", type=int, default=PAGES)
    parser.add_argument(
        "--series-days", type=int, default=60, help="days of the daily 'above ___' series to scan"
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.05,
        help="also report hypothetical trades at this edge vs model P",
    )
    args = parser.parse_args()

    async with httpx.AsyncClient(timeout=15.0) as http:
        raw = await fetch_resolved_markets(http, args.pages, args.series_days)
        print(f"scanned {len(raw)} closed markets", flush=True)

        rows = []
        for m in raw:
            q = m.get("question", "") or ""
            parsed = _parse_threshold_question(q)
            if not parsed or not parsed["binance_symbol"]:
                continue
            yes_won = parse_outcome(m)
            if yes_won is None:
                continue
            end = parse_end_date(m.get("endDate", ""))
            if end is None:
                continue
            rows.append((m, parsed, yes_won, end))

        print(f"matched {len(rows)} resolved crypto threshold markets", flush=True)

        sem = asyncio.Semaphore(10)

        async def evaluate(m, parsed, yes_won, end):
            entry_time = end - timedelta(hours=args.hours_before)
            symbol = parsed["binance_symbol"]
            end_ms = int(entry_time.timestamp() * 1000)
            start_ms = end_ms - (VOL_LOOKBACK_H + 1) * 3_600_000
            async with sem:
                closes = await fetch_klines(http, symbol, start_ms, end_ms)
                # Binance oracle at expiry (what the OLD paper mode trusted)
                expiry_closes = await fetch_klines(
                    http,
                    symbol,
                    int(end.timestamp() * 1000) - 3_600_000,
                    int(end.timestamp() * 1000) + 3_600_000,
                )
            if not closes:
                return None
            sigma = realized_daily_sigma(closes)
            if sigma is None:
                return None
            spot = closes[-1]
            t_hours = (end - entry_time).total_seconds() / 3600.0
            p_above = prob_above(spot, parsed["threshold"], t_hours, sigma)
            our_prob_yes = p_above if parsed["direction"] == "above" else (1.0 - p_above)
            binance_implies_yes = None
            if expiry_closes:
                spot_end = expiry_closes[-1]
                above = spot_end > parsed["threshold"]
                binance_implies_yes = above if parsed["direction"] == "above" else (not above)
            return {
                "question": m.get("question", "")[:70],
                "asset": parsed["asset_key"],
                "yes_won": yes_won,
                "model_p_yes": our_prob_yes,
                "binance_implies_yes": binance_implies_yes,
                "T_hours": t_hours,
                "sigma": sigma,
                "spot": spot,
                "threshold": parsed["threshold"],
            }

        tasks = [evaluate(m, p, y, e) for (m, p, y, e) in rows]
        results = [r for r in await asyncio.gather(*tasks) if r]
        print(f"evaluated {len(results)} markets with full data\n", flush=True)

        if not results:
            print("No data — try more pages.")
            return

        # --- Oracle disagreement (the paper-lie quantifier) ---
        pairs = [r for r in results if r["binance_implies_yes"] is not None]
        if pairs:
            disagree = sum(1 for r in pairs if r["binance_implies_yes"] != r["yes_won"])
            print(
                f"ORACLE DISAGREEMENT (Binance@expiry vs actual resolution): "
                f"{disagree}/{len(pairs)} = {disagree / len(pairs) * 100:.1f}%"
            )
            print("  → every disagreement point was a fabricated paper win/loss\n")

        # --- Calibration buckets ---
        buckets = {}
        for r in results:
            b = round(r["model_p_yes"] * 10) / 10  # 0.1-wide buckets
            buckets.setdefault(b, []).append(1.0 if r["yes_won"] else 0.0)
        print("CALIBRATION (model P_yes bucket → actual YES frequency):")
        print(f"{'bucket':>8} {'n':>5} {'actual':>8} {'gap':>7}")
        for b in sorted(buckets):
            outs = buckets[b]
            actual = sum(outs) / len(outs)
            print(f"{b:>8.1f} {len(outs):>5} {actual:>8.2f} {actual - b:>+7.2f}")

        # --- Directional accuracy + Brier ---
        correct = sum(1 for r in results if (r["model_p_yes"] >= 0.5) == r["yes_won"])
        brier = sum(
            (r["model_p_yes"] - (1.0 if r["yes_won"] else 0.0)) ** 2 for r in results
        ) / len(results)
        acc = correct / len(results) * 100
        print(f"\nDIRECTIONAL ACCURACY: {correct}/{len(results)} = {acc:.1f}%")
        print(f"BRIER SCORE: {brier:.4f} (0=perfect, 0.25=coin flip)")

        # --- Hypothetical strategy simulation at min-edge ---
        # Assumes the market was priced at model_p - edge (optimistic for the
        # bot). Even under this GENEROUS assumption, fee+oracle disagreement
        # must be overcome — see report.
        fee = 0.018
        trades = []
        for r in results:
            p = r["model_p_yes"]
            # Both sides: bot buys YES when model P exceeds mkt by edge, NO when below.
            mkt_yes = p - args.min_edge
            if mkt_yes > 0.02:
                # we buy YES at mkt_yes (plus fee); payout = yes_won
                cost = mkt_yes * (1 + fee)
                pnl = (1.0 if r["yes_won"] else 0.0) - cost
                trades.append((pnl, "YES"))
            mkt_no = (1.0 - p) - args.min_edge
            if mkt_no > 0.02:
                cost = mkt_no * (1 + fee)
                pnl = (0.0 if r["yes_won"] else 1.0) - cost
                trades.append((pnl, "NO"))
        if trades:
            wins = sum(1 for pnl, _ in trades if pnl > 0)
            total = sum(pnl for pnl, _ in trades)
            print(
                f"\nHYPOTHETICAL @edge={args.min_edge * 100:.0f}% (market = model - edge, "
                f"fee {fee * 100:.1f}%): "
                f"{len(trades)} trades, WR {wins / len(trades) * 100:.1f}%, "
                f"PnL/share ${total / len(trades):+.4f}"
            )
            print("  NOTE: this ASSUMES the market was cheaper than the model —")
            print("  a generous assumption. Real market prices were not recorded.")


if __name__ == "__main__":
    asyncio.run(main())
