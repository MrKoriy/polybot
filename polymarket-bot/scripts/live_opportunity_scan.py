#!/usr/bin/env python3
"""Live opportunity scan for Polymarket and exchange carry snapshots.

Usage:
    .venv/bin/python scripts/live_opportunity_scan.py
    .venv/bin/python scripts/live_opportunity_scan.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import httpx


GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
BINANCE_PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
DERIBIT_BTC_FUTURES_URL = (
    "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
    "?currency=BTC&kind=future"
)


BUCKET_TAGS: dict[str, set[str]] = {
    "weather": {"Weather"},
    "economics": {
        "Economy",
        "Fed",
        "Economic Policy",
        "Inflation",
        "Jobs",
        "Macro Indicators",
        "GDP",
        "Treasuries",
        "Fed Rates",
    },
    "crypto": {"Crypto", "Bitcoin", "Crypto Prices", "Ethereum", "Airdrops", "Pre-Market"},
    "sports": {"Sports", "Soccer", "NBA", "NHL", "NFL", "Basketball", "Hockey"},
    "politics": {
        "Politics",
        "Geopolitics",
        "Elections",
        "US Election",
        "World Elections",
        "Global Elections",
    },
    "tech": {"Tech", "AI", "Big Tech", "OpenAI", "IPO", "IPOs"},
}


MAJOR_BINANCE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
MAJOR_HYPERLIQUID_COINS = ("BTC", "ETH", "SOL", "HYPE", "XRP")


@dataclass(slots=True)
class BucketSummary:
    bucket: str
    events: int
    volume24hr: float
    total_volume: float


@dataclass(slots=True)
class RewardMarket:
    event: str
    question: str
    reward_daily: float
    volume24hr: float
    liquidity: float
    spread: float | None
    best_bid: float | None
    best_ask: float | None
    tags: list[str]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def event_tags(event: dict[str, Any]) -> list[str]:
    return [tag.get("label", "") for tag in event.get("tags") or [] if tag.get("label")]


def active_markets(event: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in (event.get("markets") or []) if m.get("active") and not m.get("closed")]


def matches_bucket(event: dict[str, Any], bucket_tags: set[str]) -> bool:
    tags = set(event_tags(event))
    return bool(tags & bucket_tags)


def build_bucket_summaries(events: list[dict[str, Any]]) -> list[BucketSummary]:
    summaries: list[BucketSummary] = []
    for bucket, tags in BUCKET_TAGS.items():
        matched = [ev for ev in events if matches_bucket(ev, tags)]
        summaries.append(
            BucketSummary(
                bucket=bucket,
                events=len(matched),
                volume24hr=sum(safe_float(ev.get("volume24hr")) for ev in matched),
                total_volume=sum(safe_float(ev.get("volume")) for ev in matched),
            )
        )
    return sorted(summaries, key=lambda item: item.volume24hr, reverse=True)


def top_events(events: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    ranked = sorted(events, key=lambda ev: safe_float(ev.get("volume24hr")), reverse=True)
    result: list[dict[str, Any]] = []
    for ev in ranked[:limit]:
        result.append(
            {
                "title": ev.get("title", ""),
                "slug": ev.get("slug", ""),
                "volume24hr": safe_float(ev.get("volume24hr")),
                "volume": safe_float(ev.get("volume")),
                "liquidity": safe_float(ev.get("liquidity")),
                "tags": event_tags(ev)[:8],
            }
        )
    return result


def top_reward_markets(events: list[dict[str, Any]], limit: int = 20) -> list[RewardMarket]:
    rows: list[RewardMarket] = []
    for ev in events:
        tags = event_tags(ev)
        for market in active_markets(ev):
            reward_daily = sum(safe_float(r.get("rewardsDailyRate")) for r in market.get("clobRewards") or [])
            if reward_daily <= 0:
                continue
            rows.append(
                RewardMarket(
                    event=ev.get("title", ""),
                    question=market.get("question", ""),
                    reward_daily=reward_daily,
                    volume24hr=safe_float(market.get("volume24hr")),
                    liquidity=safe_float(market.get("liquidityNum")),
                    spread=safe_float(market.get("spread"), default=None),
                    best_bid=safe_float(market.get("bestBid"), default=None),
                    best_ask=safe_float(market.get("bestAsk"), default=None),
                    tags=tags[:6],
                )
            )
    rows.sort(key=lambda row: (row.reward_daily, row.volume24hr), reverse=True)
    return rows[:limit]


def reward_candidates(
    rewards: list[RewardMarket],
    *,
    min_reward_daily: float = 30.0,
    max_spread: float = 0.02,
    min_volume24hr: float = 5000.0,
    limit: int = 12,
) -> list[RewardMarket]:
    filtered = [
        row
        for row in rewards
        if row.reward_daily >= min_reward_daily
        and row.spread is not None
        and row.spread <= max_spread
        and row.volume24hr >= min_volume24hr
    ]
    filtered.sort(key=lambda row: (row.reward_daily, row.volume24hr), reverse=True)
    return filtered[:limit]


def weather_snapshot(events: list[dict[str, Any]]) -> dict[str, Any]:
    weather_events = [ev for ev in events if matches_bucket(ev, BUCKET_TAGS["weather"])]
    output: list[dict[str, Any]] = []
    for ev in weather_events:
        markets = []
        for market in active_markets(ev):
            markets.append(
                {
                    "question": market.get("question", ""),
                    "price": safe_float(market.get("lastTradePrice")),
                    "bestBid": safe_float(market.get("bestBid"), default=None),
                    "bestAsk": safe_float(market.get("bestAsk"), default=None),
                    "spread": safe_float(market.get("spread"), default=None),
                    "volume24hr": safe_float(market.get("volume24hr")),
                    "liquidity": safe_float(market.get("liquidityNum")),
                    "rewardDaily": sum(
                        safe_float(r.get("rewardsDailyRate")) for r in market.get("clobRewards") or []
                    ),
                    "negRisk": bool(market.get("negRisk")),
                }
            )
        output.append(
            {
                "title": ev.get("title", ""),
                "slug": ev.get("slug", ""),
                "volume24hr": safe_float(ev.get("volume24hr")),
                "volume": safe_float(ev.get("volume")),
                "liquidity": safe_float(ev.get("liquidity")),
                "markets": markets,
            }
        )
    return {"events": len(weather_events), "items": output}


def find_event(events: list[dict[str, Any]], slug: str) -> dict[str, Any] | None:
    for ev in events:
        if ev.get("slug") == slug:
            return ev
    return None


def extract_named_event_markets(event: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not event:
        return []
    rows = []
    for market in active_markets(event):
        rows.append(
            {
                "question": market.get("question", ""),
                "price": safe_float(market.get("lastTradePrice")),
                "bestBid": safe_float(market.get("bestBid"), default=None),
                "bestAsk": safe_float(market.get("bestAsk"), default=None),
                "spread": safe_float(market.get("spread"), default=None),
                "volume24hr": safe_float(market.get("volume24hr")),
                "liquidity": safe_float(market.get("liquidityNum")),
            }
        )
    return rows


async def fetch_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    if json_body is not None:
        response = await client.post(url, json=json_body)
    else:
        response = await client.get(url, params=params)
    response.raise_for_status()
    return response.json()


async def fetch_polymarket_events(
    client: httpx.AsyncClient,
    limit: int = 500,
    max_pages: int = 30,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[str] = set()

    for page in range(max_pages):
        payload = await fetch_json(
            client,
            GAMMA_EVENTS_URL,
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": page * limit,
            },
        )
        if not payload:
            break

        for event in payload:
            key = str(event.get("id") or event.get("slug") or "")
            if key and key not in seen:
                events.append(event)
                seen.add(key)

        if len(payload) < limit:
            break

    return events


def is_city_weather_event(event: dict[str, Any]) -> bool:
    title = str(event.get("title") or "")
    tag_set = set(event_tags(event))
    has_city_weather_title = (
        "Highest temperature" in title
        or "Lowest temperature" in title
        or "precipitation" in title.lower()
    )
    return has_city_weather_title and "Recurring" in tag_set and "Weather" in tag_set


def event_is_tradable(event: dict[str, Any]) -> bool:
    end_date = event.get("endDate")
    if not end_date:
        return True
    try:
        return datetime.fromisoformat(end_date.replace("Z", "+00:00")) > datetime.now(UTC)
    except ValueError:
        return True


def city_weather_snapshot(events: list[dict[str, Any]]) -> dict[str, Any]:
    all_events = [ev for ev in events if is_city_weather_event(ev)]
    tradable_events = [ev for ev in all_events if event_is_tradable(ev)]

    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        markets = [market for event in items for market in active_markets(event)]
        return {
            "events": len(items),
            "markets": len(markets),
            "volume24hr": sum(safe_float(event.get("volume24hr")) for event in items),
            "liquidity": sum(safe_float(event.get("liquidity")) for event in items),
        }

    top_events = []
    for event in sorted(all_events, key=lambda ev: safe_float(ev.get("volume24hr")), reverse=True)[:30]:
        top_events.append(
            {
                "title": event.get("title", ""),
                "slug": event.get("slug", ""),
                "endDate": event.get("endDate"),
                "tradable": event_is_tradable(event),
                "volume24hr": safe_float(event.get("volume24hr")),
                "liquidity": safe_float(event.get("liquidity")),
                "marketCount": len(active_markets(event)),
            }
        )

    return {
        "all": summarize(all_events),
        "tradable": summarize(tradable_events),
        "topEvents": top_events,
    }


def format_binance_funding(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized = [
        {
            "symbol": row.get("symbol", ""),
            "markPrice": safe_float(row.get("markPrice")),
            "lastFundingRate": safe_float(row.get("lastFundingRate")),
            "nextFundingTime": row.get("nextFundingTime"),
        }
        for row in rows
    ]
    major = [row for row in normalized if row["symbol"] in MAJOR_BINANCE_SYMBOLS]
    top_abs = sorted(normalized, key=lambda row: abs(row["lastFundingRate"]), reverse=True)[:20]
    return major, top_abs


def format_hyperliquid_funding(payload: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    universe = payload[0]["universe"]
    contexts = payload[1]
    rows = []
    for idx, asset in enumerate(universe):
        ctx = contexts[idx]
        rows.append(
            {
                "coin": asset.get("name", ""),
                "funding": safe_float(ctx.get("funding")),
                "markPx": safe_float(ctx.get("markPx")),
                "openInterest": safe_float(ctx.get("openInterest")),
            }
        )
    major = [row for row in rows if row["coin"] in MAJOR_HYPERLIQUID_COINS]
    top_abs = sorted(rows, key=lambda row: abs(row["funding"]), reverse=True)[:20]
    return major, top_abs


def deribit_basis_snapshot(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result") or []
    perpetual = next((row for row in result if row.get("instrument_name") == "BTC-PERPETUAL"), None)
    if not perpetual:
        return []

    spot = safe_float(perpetual.get("estimated_delivery_price"))
    if spot <= 0:
        return []

    now = datetime.now(UTC)
    rows = []
    for row in result:
        name = row.get("instrument_name", "")
        if "PERPETUAL" in name:
            continue
        expiry_token = name.split("-")[-1]
        try:
            expiry = datetime.strptime(expiry_token, "%d%b%y").replace(tzinfo=UTC)
        except ValueError:
            continue
        days = (expiry - now).total_seconds() / 86400
        if days <= 1:
            continue
        fut = safe_float(row.get("mark_price"))
        if fut <= 0:
            continue
        basis = fut / spot - 1.0
        annualized = basis * (365.0 / days)
        rows.append(
            {
                "instrument": name,
                "daysToExpiry": round(days, 1),
                "markPrice": fut,
                "spotRef": spot,
                "basisPct": round(basis * 100, 3),
                "annualizedPct": round(annualized * 100, 3),
                "openInterest": safe_float(row.get("open_interest")),
                "volumeUsd": safe_float(row.get("volume_usd")),
            }
        )
    rows.sort(key=lambda item: item["daysToExpiry"])
    return rows[:8]


async def build_snapshot(limit: int = 500, max_pages: int = 30) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        poly_task = fetch_polymarket_events(client, limit=limit, max_pages=max_pages)
        binance_task = fetch_json(client, BINANCE_PREMIUM_INDEX_URL)
        hyper_task = fetch_json(client, HYPERLIQUID_INFO_URL, json_body={"type": "metaAndAssetCtxs"})
        deribit_task = fetch_json(client, DERIBIT_BTC_FUTURES_URL)

        events, binance_payload, hyper_payload, deribit_payload = await asyncio.gather(
            poly_task,
            binance_task,
            hyper_task,
            deribit_task,
        )

    rewards = top_reward_markets(events)
    binance_major, binance_top_abs = format_binance_funding(binance_payload)
    hyper_major, hyper_top_abs = format_hyperliquid_funding(hyper_payload)

    return {
        "asOfUtc": datetime.now(UTC).isoformat(),
        "polymarket": {
            "activeEventsFetched": len(events),
            "bucketSummaries": [asdict(item) for item in build_bucket_summaries(events)],
            "topEventsBy24hVolume": top_events(events),
            "topRewardMarkets": [asdict(item) for item in rewards],
            "rewardCandidates": [asdict(item) for item in reward_candidates(rewards)],
            "weather": weather_snapshot(events),
            "cityWeather": city_weather_snapshot(events),
            "fedDecisionApril": extract_named_event_markets(find_event(events, "fed-decision-in-april")),
            "weedRescheduled": extract_named_event_markets(find_event(events, "weed-rescheduled-by-march-31")),
        },
        "exchanges": {
            "binanceMajors": binance_major,
            "binanceTopAbsFunding": binance_top_abs,
            "hyperliquidMajors": hyper_major,
            "hyperliquidTopAbsFunding": hyper_top_abs,
            "deribitBtcBasis": deribit_basis_snapshot(deribit_payload),
        },
    }


def fmt_money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def print_snapshot(snapshot: dict[str, Any]) -> None:
    print("=" * 72)
    print("LIVE OPPORTUNITY SCAN")
    print("=" * 72)
    print(f"As of UTC: {snapshot['asOfUtc']}")
    print()

    print("Polymarket directions by 24h flow")
    for row in snapshot["polymarket"]["bucketSummaries"]:
        print(
            f"- {row['bucket']:10s} events={row['events']:>3} "
            f"24h={fmt_money(row['volume24hr'])} total={fmt_money(row['total_volume'])}"
        )
    print()

    print("Top active events by 24h volume")
    for row in snapshot["polymarket"]["topEventsBy24hVolume"][:10]:
        print(
            f"- {row['title']} | 24h={fmt_money(row['volume24hr'])} "
            f"liq={fmt_money(row['liquidity'])} | tags={', '.join(row['tags'][:4])}"
        )
    print()

    print("Reward-heavy markets")
    for row in snapshot["polymarket"]["topRewardMarkets"][:12]:
        spread = "n/a" if row["spread"] is None else f"{row['spread']:.3f}"
        print(
            f"- reward/day={row['reward_daily']:.3f} | {row['question']} | "
            f"24h={fmt_money(row['volume24hr'])} liq={fmt_money(row['liquidity'])} spread={spread}"
        )
    print()

    print("Reward candidates (tight spread + live volume)")
    for row in snapshot["polymarket"]["rewardCandidates"][:12]:
        print(
            f"- reward/day={row['reward_daily']:.0f} | bid={row['best_bid']:.3f} ask={row['best_ask']:.3f} "
            f"| {row['question']}"
        )
    print()

    weather = snapshot["polymarket"]["weather"]
    print(f"Weather status: {weather['events']} active weather-tagged events")
    for item in weather["items"]:
        print(f"- {item['title']} | 24h={fmt_money(item['volume24hr'])} liq={fmt_money(item['liquidity'])}")
        for market in item["markets"][:3]:
            spread = "n/a" if market["spread"] is None else f"{market['spread']:.3f}"
            print(
                f"  · {market['question']} | px={market['price']:.3f} "
                f"bid={market['bestBid']} ask={market['bestAsk']} spread={spread} "
                f"reward/day={market['rewardDaily']:.3f}"
            )
    print()

    city_weather = snapshot["polymarket"]["cityWeather"]
    all_city = city_weather["all"]
    tradable_city = city_weather["tradable"]
    print(
        "City-weather status: "
        f"{all_city['events']} events / {all_city['markets']} markets, "
        f"24h={fmt_money(all_city['volume24hr'])}; "
        f"tradable={tradable_city['events']} events / {tradable_city['markets']} markets, "
        f"24h={fmt_money(tradable_city['volume24hr'])}"
    )
    for row in city_weather["topEvents"][:12]:
        state = "open" if row["tradable"] else "ended"
        print(
            f"- {state:5s} | {row['title']} | 24h={fmt_money(row['volume24hr'])} "
            f"liq={fmt_money(row['liquidity'])} markets={row['marketCount']}"
        )
    print()

    print("Fed decision in April snapshot")
    for row in snapshot["polymarket"]["fedDecisionApril"]:
        spread = "n/a" if row["spread"] is None else f"{row['spread']:.3f}"
        print(
            f"- {row['question']} | px={row['price']:.3f} bid={row['bestBid']} "
            f"ask={row['bestAsk']} spread={spread}"
        )
    print()

    print("Weed rescheduling snapshot")
    for row in snapshot["polymarket"]["weedRescheduled"]:
        spread = "n/a" if row["spread"] is None else f"{row['spread']:.3f}"
        print(
            f"- {row['question']} | px={row['price']:.3f} bid={row['bestBid']} "
            f"ask={row['bestAsk']} spread={spread}"
        )
    print()

    print("Exchange carry snapshot")
    print("Binance majors")
    for row in snapshot["exchanges"]["binanceMajors"]:
        print(f"- {row['symbol']}: funding={row['lastFundingRate']:.8f} mark={row['markPrice']:.4f}")
    print("Hyperliquid majors")
    for row in snapshot["exchanges"]["hyperliquidMajors"]:
        print(f"- {row['coin']}: funding={row['funding']:.8f} mark={row['markPx']:.4f}")
    print("Deribit BTC basis")
    for row in snapshot["exchanges"]["deribitBtcBasis"]:
        print(
            f"- {row['instrument']}: days={row['daysToExpiry']:.1f} "
            f"basis={row['basisPct']:.3f}% annualized={row['annualizedPct']:.3f}%"
        )


async def async_main(args: argparse.Namespace) -> int:
    snapshot = await build_snapshot(limit=args.limit, max_pages=args.max_pages)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=True, indent=2))
    else:
        print_snapshot(snapshot)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=500, help="Gamma events limit")
    parser.add_argument("--max-pages", type=int, default=30, help="Maximum Gamma event pages")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of text")
    return parser.parse_args()


def main() -> int:
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
