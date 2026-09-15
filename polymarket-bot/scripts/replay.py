"""Replay production trades.db through the new BS model and compare PnL.

Usage: python scripts/replay.py [path/to/trades.db]

This script pulls THRESHOLD trades and reconstructs the signal:
  - Input: S (entry spot via reasoning or estimate), K (threshold), T_hours, direction, price.
  - Replays prob_above() under different MIN_EDGE / Kelly configs.
  - Reports how many additional signals would have fired under the new regime
    and what the realised PnL distribution looked like.

Limitations: we only have S_entry encoded in the reasoning string for recent trades.
For older threshold trades we skip. This is a rough first-order backtest — it shows
whether the new model would keep the big winners and cut the 5m/15m losers.
"""
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# Allow direct import of threshold_trader
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import types
for m in ("data.binance_ws", "storage.repository", "trading.portfolio"):
    if m not in sys.modules:
        sys.modules[m] = types.ModuleType(m)
sys.modules["data.binance_ws"].BinanceWSFeed = type("BinanceWSFeed", (), {})
sys.modules["storage.repository"].TradeRepository = type("TradeRepository", (), {})
sys.modules["trading.portfolio"].Portfolio = type("Portfolio", (), {})

import importlib.util
spec = importlib.util.spec_from_file_location("tt", ROOT / "bot" / "threshold_trader.py")
tt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tt)

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "data" / "trades.db")


def parse_reasoning(reason: str) -> tuple[float, float] | None:
    """Extract entry spot + threshold from legacy reasoning strings like
    'Binance $65,432.10 vs threshold $66,000 (-0.9%)'.
    Returns (S, K) or None if not parseable.
    """
    if not reason:
        return None
    m = re.search(r"\$([\d,]+(?:\.\d+)?).*?\$([\d,]+(?:\.\d+)?)", reason)
    if not m:
        return None
    try:
        s = float(m.group(1).replace(",", ""))
        k = float(m.group(2).replace(",", ""))
        return s, k
    except ValueError:
        return None


def main():
    if not os.path.exists(DB_PATH):
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)
    c = sqlite3.connect(DB_PATH)

    print("=" * 70)
    print(f"REPLAY: {DB_PATH}")
    print("=" * 70)

    print("\n--- Production baseline ---")
    rows = list(c.execute("""
        SELECT question, side, direction, price, size, edge, pnl, estimated_prob,
               reasoning, created_at, closed_at
        FROM trades
        WHERE status='closed'
    """))
    print(f"Total closed trades: {len(rows)}")

    # Group by strategy prefix
    groups = defaultdict(list)
    for r in rows:
        q = r[0] or ""
        if q.startswith("THRESH"):
            g = "THRESHOLD"
        elif re.match(r"^(BTC|ETH|SOL|XRP|DOGE) 5m", q):
            g = "crypto_5m"
        elif re.match(r"^(BTC|ETH|SOL|XRP|DOGE) 15m", q):
            g = "crypto_15m"
        else:
            g = "other"
        groups[g].append(r)

    for g, gr in sorted(groups.items(), key=lambda x: -sum(r[6] for r in x[1])):
        wins = sum(1 for r in gr if r[6] > 0)
        pnl = sum(r[6] for r in gr)
        size = sum(r[4] for r in gr)
        roi = pnl / size * 100 if size else 0
        print(f"  {g:12s} n={len(gr):4d} WR={wins/len(gr)*100:5.1f}% "
              f"PnL=${pnl:8.2f} ROI={roi:6.2f}%")

    # Focus on THRESHOLD — replay with BS model at different MIN_EDGE levels.
    thr = groups["THRESHOLD"]
    print(f"\n--- THRESHOLD replay (n={len(thr)}) ---")

    # For each trade: reconstruct S, K from reasoning, infer T_hours.
    # We don't have market endDate per trade, so we use "next day noon UTC" as
    # T_hours proxy — fine for a directional comparison.
    configs = {
        "old_step_min_edge_5pct": {"min_edge": 0.05, "use_bs": False},
        "new_bs_min_edge_3pct":   {"min_edge": 0.03, "use_bs": True},
        "new_bs_min_edge_5pct":   {"min_edge": 0.05, "use_bs": True},
        "new_bs_min_edge_2pct":   {"min_edge": 0.02, "use_bs": True},
    }

    for cfg_name, cfg in configs.items():
        kept = 0
        kept_pnl = 0.0
        filtered = 0
        for r in thr:
            q, side, direction, price, size, edge, pnl, est, reason, created, closed = r
            sk = parse_reasoning(reason or "")
            if not sk:
                continue
            S, K = sk
            # Assume 24h to expiry (threshold markets are mostly daily).
            T = 24.0
            # Use fallback vol (no klines lookup for historical replay).
            asset_match = re.search(r"THRESH (\w+)", q)
            asset = asset_match.group(1) if asset_match else "BTC"
            sigma = tt._FALLBACK_SIGMA_DAILY.get(asset, 0.04)

            is_above = ">" in q
            if cfg["use_bs"]:
                p_above = tt.prob_above(S, K, T, sigma)
                our_p = p_above if is_above else (1.0 - p_above)
            else:
                # Old step function
                dist = (S - K) / K * 100
                if dist > 5: p_a = 0.95
                elif dist > 2: p_a = 0.85
                elif dist > 0.5: p_a = 0.70
                elif dist > -0.5: p_a = 0.50
                elif dist > -2: p_a = 0.30
                elif dist > -5: p_a = 0.15
                else: p_a = 0.05
                our_p = p_a if is_above else 1.0 - p_a

            # Bet was BUY_YES or BUY_NO — edge on chosen side:
            is_buy_yes = direction.upper().endswith("YES")
            chosen_prob = our_p if is_buy_yes else (1.0 - our_p)
            model_edge = chosen_prob - price

            if model_edge >= cfg["min_edge"]:
                kept += 1
                kept_pnl += pnl
            else:
                filtered += 1

        print(f"  {cfg_name:30s} kept={kept:3d} filtered={filtered:3d} "
              f"kept_pnl=${kept_pnl:+8.2f}")

    # Simulate: "what if losing strategies had been OFF?"
    print(f"\n--- 'P1 applied (losers disabled)' ---")
    net = sum(r[6] for r in groups["THRESHOLD"]) + sum(r[6] for r in groups["other"])
    print(f"  Threshold+other only: ${net:+.2f} (vs baseline "
          f"${sum(r[6] for r in rows):+.2f})")
    print(f"  Savings: ${net - sum(r[6] for r in rows):+.2f}")


if __name__ == "__main__":
    main()
