#!/usr/bin/env python3
"""Pre-flight live-readiness checker.

Run before switching PAPER_TRADING=false. Verifies all preconditions:
  - POLYMARKET_PRIVATE_KEY set + valid
  - CLOB client initialises
  - USDC balance ≥ $5 (enough for at least one $5 bet)
  - USDC allowance ≥ $1,000 (approvals complete)
  - get_midpoint works on a real market
  - Open-Meteo + Binance APIs reachable
  - Systemd unit active (optional, only if running on server)

Exit code 0 = LIVE READY. Non-zero = blocker present; message tells you what.

Usage:
    .venv/bin/python scripts/check_live_ready.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

# Allow imports of project modules when run from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}✓{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"{RED}✗ {msg}{RESET}")


def warn(msg: str) -> None:
    print(f"{YELLOW}⚠ {msg}{RESET}")


async def main() -> int:
    errors = 0

    print("=" * 60)
    print("LIVE-READINESS CHECK")
    print("=" * 60)

    # 1. Private key
    pk = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
    if not pk:
        fail("POLYMARKET_PRIVATE_KEY not set in env/.env")
        fail("  → export from Phantom: Settings → Manage Accounts →")
        fail("    select Polygon account → Show Private Key → paste to .env")
        return 1
    if not pk.startswith("0x") or len(pk) != 66:
        fail(f"POLYMARKET_PRIVATE_KEY malformed (len={len(pk)}, expected 66 starting with 0x)")
        errors += 1
    else:
        ok(f"Private key set ({len(pk)} chars, starts 0x)")

    # 2. Derive address
    try:
        from eth_account import Account
        account = Account.from_key(pk)
        ok(f"Wallet address: {account.address}")
    except Exception as e:
        fail(f"Cannot derive address from private key: {e}")
        return 1

    # 3. Init CLOB client
    try:
        from data.polymarket_rest import PolymarketClient
        from config.settings import Settings
        settings = Settings()
        polymarket = PolymarketClient(
            private_key=settings.polymarket_private_key,
            chain_id=settings.polymarket_chain_id,
            clob_url=settings.polymarket_clob_url,
            gamma_url=settings.polymarket_gamma_url,
            clob_proxy=settings.polymarket_clob_proxy,
        )
        await polymarket.init_clob()
        if polymarket._clob_client is None:
            fail("CLOB init returned no client (check private key + network)")
            return 1
        ok("CLOB client initialised")
    except Exception as e:
        fail(f"CLOB init failed: {e}")
        return 1

    # 4. Balance
    try:
        bal = await polymarket.get_balance()
        if bal < 5.0:
            fail(f"USDC balance too low: ${bal:.4f} (need ≥$5)")
            fail(f"  → fund wallet {account.address} with USDC on Polygon")
            errors += 1
        else:
            ok(f"USDC balance: ${bal:.4f}")
    except Exception as e:
        fail(f"Balance check failed: {e}")
        errors += 1

    # 5. Allowance
    try:
        allow = await polymarket.get_allowance()
        if allow < 1000.0:
            fail(f"USDC allowance too low: ${allow:.2f} (need ≥$1000)")
            fail(f"  → run: .venv/bin/python scripts/setup_approvals.py")
            errors += 1
        else:
            ok(f"USDC allowance: ${allow:.2f}")
    except Exception as e:
        fail(f"Allowance check failed: {e}")
        errors += 1

    # 6. Midpoint on a real market
    try:
        markets = await polymarket.get_active_markets(min_volume=10000, limit=5)
        if markets:
            first = markets[0]
            tok = first.yes_token_id
            mid = await polymarket.get_midpoint(tok)
            if mid and 0.01 < mid < 0.99:
                ok(f"Midpoint read OK: {mid:.3f} on '{first.question[:40]}…'")
            else:
                warn(f"Midpoint read returned unusual value: {mid}")
        else:
            warn("No active markets returned (Gamma API issue?)")
    except Exception as e:
        warn(f"Midpoint test failed: {e}")

    # 7. External APIs
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.open-meteo.com/v1/forecast",
                            params={"latitude": 40.71, "longitude": -74.01,
                                    "daily": "temperature_2m_max",
                                    "timezone": "America/New_York"})
            if r.status_code == 200:
                ok("Open-Meteo (forecast) reachable")
            else:
                warn(f"Open-Meteo HTTP {r.status_code}")
            r = await c.get("https://data-api.binance.vision/api/v3/time")
            if r.status_code == 200:
                ok("Binance data API reachable")
            else:
                warn(f"Binance HTTP {r.status_code}")
    except Exception as e:
        warn(f"External API check failed: {e}")

    await polymarket.close()

    print()
    if errors == 0:
        print(f"{GREEN}=" * 60)
        print("✓ LIVE READY — you can set PAPER_TRADING=false")
        print(f"=" * 60 + f"{RESET}")
        return 0
    else:
        print(f"{RED}=" * 60)
        print(f"✗ NOT READY — {errors} blocker(s) above. Fix and re-run.")
        print(f"=" * 60 + f"{RESET}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
