"""Honest market-price backtest for the threshold strategy.

Unlike backtest_threshold.py (model calibration only), this replays the LIVE
signal function `score_threshold_signal` against:
  * real Polymarket hourly YES prices (CLOB /prices-history),
  * actual market resolutions (Gamma outcomePrices),
  * Binance 1h klines for spot + realised vol (no look-ahead: last closed candle),
  * the crypto taker fee rate*p*(1-p) and a simulated half-spread around mid.

Data is cached under data/bt_cache/. Usage:
    python scripts/backtest_threshold_market.py --days 140 [--half-spread 0.01] [--split 2026-08-15]

History has no bid/ask, so we drop the 0.5 listing placeholder and require the
price to have moved in the last 3 hours (a proxy for a live book).
"""
import argparse
import asyncio
import bisect
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot.threshold_trader as tt  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
KLINES = "https://data-api.binance.vision/api/v3/klines"
CACHE = Path(__file__).resolve().parents[1] / "data" / "bt_cache"
MONTHS = ("january february march april may june july august september "
          "october november december").split()
ASSETS = {"bitcoin": "BTCUSDT", "ethereum": "ETHUSDT", "solana": "SOLUSDT", "xrp": "XRPUSDT"}
STRIKE_RE = re.compile(r"above \$([\d,\.]+)", re.I)


async def _get(http, sem, url, params, tries=4):
    for i in range(tries):
        try:
            async with sem:
                r = await http.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            await asyncio.sleep(1 + 2 * i)
        except Exception:
            await asyncio.sleep(1 + i)
    return None


async def collect(days: int) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(16)
    today = datetime.now(timezone.utc).date()
    async with httpx.AsyncClient(timeout=20) as http:
        for sym in ASSETS.values():
            f = CACHE / f"k_{sym}.json"
            if f.exists() and f.stat().st_mtime > datetime.now().timestamp() - 86400:
                continue
            end = int(datetime.now(timezone.utc).timestamp() * 1000)
            t, rows = end - (days + 40) * 86_400_000, []
            while t < end:
                k = await _get(http, sem, KLINES,
                               {"symbol": sym, "interval": "1h", "startTime": t, "limit": 1000})
                if not k:
                    break
                rows += [[x[0], float(x[4])] for x in k]
                t = k[-1][0] + 3_600_000
            f.write_text(json.dumps(rows))

        async def event(asset, d):
            slug = f"{asset}-above-on-{MONTHS[d.month - 1]}-{d.day}-{d.year}"
            f = CACHE / f"{slug}.json"
            if f.exists():
                return
            ev = await _get(http, sem, f"{GAMMA}/events", {"slug": slug})
            if not ev:
                f.write_text("null")
                return
            out = []
            for m in ev[0].get("markets", []):
                end = int(datetime.fromisoformat(m["endDate"].replace("Z", "+00:00")).timestamp())
                tok = json.loads(m["clobTokenIds"])[0]
                h = await _get(http, sem, f"{CLOB}/prices-history",
                               {"market": tok, "startTs": end - 8 * 86400,
                                "endTs": end + 3600, "fidelity": 60})
                out.append({"question": m["question"], "outcomePrices": m.get("outcomePrices"),
                            "endDate": m["endDate"], "volume": m.get("volumeNum"),
                            "hist": (h or {}).get("history", [])})
            f.write_text(json.dumps({"asset": asset, "markets": out}))

        await asyncio.gather(*[event(a, today - timedelta(days=i))
                               for i in range(1, days + 1) for a in ASSETS])


def load():
    spot = {}
    for a, sym in ASSETS.items():
        rows = json.loads((CACHE / f"k_{sym}.json").read_text())
        spot[a] = ([r[0] // 1000 for r in rows], [r[1] for r in rows])
    markets = []
    for f in CACHE.glob("*-above-on-*.json"):
        d = json.loads(f.read_text())
        if not d:
            continue
        for m in d["markets"]:
            op = m["outcomePrices"]
            op = json.loads(op) if isinstance(op, str) else op
            km = STRIKE_RE.search(m["question"])
            if not op or float(op[0]) not in (0.0, 1.0) or not km:
                continue
            h = sorted((x["t"], x["p"]) for x in m["hist"])
            while h and h[0][1] == 0.5:  # listing placeholder, not a quote
                h.pop(0)
            if len(h) < 4:
                continue
            h = h[1:]
            markets.append({
                "asset": d["asset"], "K": float(km.group(1).replace(",", "")),
                "end": int(datetime.fromisoformat(m["endDate"].replace("Z", "+00:00")).timestamp()),
                "yes_won": float(op[0]) == 1.0, "vol": m.get("volume") or 0,
                "ht": [x[0] for x in h], "hp": [x[1] for x in h], "day": m["endDate"][:10],
            })
    return spot, markets


def _sigma(closes, i, n):
    if i - n < 1:
        return None
    r = [math.log(closes[j] / closes[j - 1]) for j in range(i - n + 1, i + 1)]
    mu = sum(r) / n
    return math.sqrt(sum((x - mu) ** 2 for x in r) / (n - 1) * 24)


def simulate(spot, markets, half_spread=0.01):
    trades = []
    for m in markets:
        if m["vol"] < tt.MIN_VOLUME:
            continue
        ts, cs = spot[m["asset"]]
        t = m["end"] - int(tt.MAX_HOURS_TO_EXPIRY * 3600)
        t -= t % 3600
        while t < m["end"] - tt.MIN_HOURS_TO_EXPIRY * 3600:
            j = bisect.bisect_right(m["ht"], t) - 1
            i = bisect.bisect_right(ts, t - 3600) - 1  # last CLOSED 1h candle
            fresh = j >= 3 and t - m["ht"][j] <= 3 * 3600
            if fresh and len(set(m["hp"][j - 3:j + 1])) > 1 and i > 0:
                sd = _sigma(cs, i, tt.VOL_CANDLES_HOURS)
                mid = m["hp"][j]
                if sd:
                    sig = tt.score_threshold_signal(
                        spot=cs[i], threshold=m["K"], is_above=True,
                        t_hours=(m["end"] - t) / 3600, sigma_daily=sd,
                        best_bid=max(0.001, mid - half_spread),
                        best_ask=min(0.999, mid + half_spread))
                    if sig:
                        win = m["yes_won"] if sig["side"] == "YES" else not m["yes_won"]
                        cost = sig["price"] + sig["fee"]
                        trades.append({"win": win, "cost": cost, "pnl": float(win) - cost,
                                       "asset": m["asset"], "side": sig["side"], "day": m["day"]})
                        break  # one entry per market, hold to resolution
            t += 3600
    return trades


def line(label, tr):
    if not tr:
        return f"{label:28s} n=0"
    n, w = len(tr), sum(x["win"] for x in tr)
    pnl, cost = sum(x["pnl"] for x in tr), sum(x["cost"] for x in tr)
    return (f"{label:28s} n={n:5d}  WR={w / n:6.1%}  ROI={pnl / cost:+7.2%}  "
            f"pnl/share={pnl / n:+.4f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=140)
    ap.add_argument("--half-spread", type=float, default=0.01)
    ap.add_argument("--split", default="2026-08-15", help="train/test boundary date")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    if not a.no_fetch:
        asyncio.run(collect(a.days))
    spot, markets = load()
    tr = simulate(spot, markets, a.half_spread)
    print(f"resolved markets: {len(markets)}")
    print(line("ALL", tr))
    print(line(f"before {a.split}", [x for x in tr if x["day"] < a.split]))
    print(line(f"from {a.split} (OOS)", [x for x in tr if x["day"] >= a.split]))
    for key in ("asset", "side"):
        g = defaultdict(list)
        for x in tr:
            g[x[key]].append(x)
        for k in sorted(g):
            print(line(f"  {key}={k}", g[k]))
    g = defaultdict(list)
    for x in tr:
        g[x["day"][:7]].append(x)
    for k in sorted(g):
        print(line(f"  month {k}", g[k]))


if __name__ == "__main__":
    main()
