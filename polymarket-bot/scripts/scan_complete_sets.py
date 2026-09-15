#!/usr/bin/env python3
"""Read-only scan for capital-safe complete-set maker quotes."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.complete_set_maker import CompleteSetScanner, MakerScannerConfig  # noqa: E402
from data.polymarket_rest import PolymarketClient  # noqa: E402


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bankroll", type=float, default=100.0)
    parser.add_argument("--hours", type=float, default=72.0)
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--min-volume", type=float, default=2_000.0)
    parser.add_argument("--max-markets", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    client = PolymarketClient()
    scanner = CompleteSetScanner(
        client,
        bankroll_getter=lambda: args.bankroll,
        config=MakerScannerConfig(
            max_resolution_hours=args.hours,
            event_pages=args.pages,
            min_volume_24h=args.min_volume,
            max_markets=args.max_markets,
        ),
    )
    try:
        opportunities = await scanner.scan()
        allocated = scanner.allocate(opportunities)
        payload = {
            "mode": "read_only_shadow",
            "bankroll": args.bankroll,
            "opportunities": len(opportunities),
            "allocated": len(allocated),
            "reserve_usd": round(sum(row.reserve_usd for row in allocated), 4),
            "locked_edge_if_both_fill_usd": round(
                sum(row.locked_edge_usd for row in allocated), 4
            ),
            "quotes": [row.to_dict() for row in allocated],
        }
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(
                f"Shadow scan: {len(opportunities)} candidates, "
                f"{len(allocated)} allocated, reserve ${payload['reserve_usd']:.2f}, "
                f"locked edge if both legs fill ${payload['locked_edge_if_both_fill_usd']:.2f}"
            )
            for index, row in enumerate(allocated, start=1):
                print(
                    f"{index}. {row.question[:76]}\n"
                    f"   YES {row.quote_yes:.3f} + NO {row.quote_no:.3f} "
                    f"x {row.shares:g} | edge {row.gross_edge:.2%} "
                    f"(${row.locked_edge_usd:.3f}) | reserve ${row.reserve_usd:.2f} "
                    f"| emergency ${row.worst_emergency_loss_usd:.3f}"
                )
        return 0
    except Exception as exc:
        print(f"Shadow scan failed without placing orders: {exc}", file=sys.stderr)
        return 2
    finally:
        await scanner.close()
        await client.close()


def main() -> int:
    return asyncio.run(_run(_args()))


if __name__ == "__main__":
    raise SystemExit(main())
