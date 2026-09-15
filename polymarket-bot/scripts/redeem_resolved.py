#!/usr/bin/env python3
"""Redeem winning CTF outcome tokens after Polymarket markets resolve.

After a market endDate passes and UMA finalises the outcome, winning outcome
tokens become redeemable 1:1 for USDC via the CTF contract. The CLOB
orderbook goes illiquid post-resolution (only spam bids at $0.001), so bot's
close_live path fails. This script bypasses CLOB and calls the on-chain
redeem function directly.

Handles two market types:
  - Standard:  CTFExchange → CTF.redeemPositions(USDC, 0x0, conditionId, [1,2])
  - NegRisk:   NegRiskAdapter.redeemPositions(conditionId, [yesAmount, noAmount])

Usage:
    .venv/bin/python scripts/redeem_resolved.py              # dry-run (report only)
    .venv/bin/python scripts/redeem_resolved.py --execute   # actually submit redemptions

Run as cron (every 30 min recommended):
    */30 * * * * cd /root/polymarket-bot && .venv/bin/python scripts/redeem_resolved.py --execute >> /var/log/redeem.log 2>&1
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

# Reuse RPC + tx helpers from setup_approvals
_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
import importlib.util as _ilu
_sa_spec = _ilu.spec_from_file_location("setup_approvals", _SCRIPTS_DIR / "setup_approvals.py")
_sa = _ilu.module_from_spec(_sa_spec)
_sa_spec.loader.exec_module(_sa)

# Contract addresses — Polygon mainnet
USDC = "0x2791Bca1f2de4661ED88A30C99a7a9449Aa84174"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CHAIN_ID = 137

# Function selectors (first 4 bytes of keccak256(sig))
# CTF.redeemPositions(address,bytes32,bytes32,uint256[])
CTF_REDEEM_SELECTOR = "0xb9dd7b0f"
# NegRiskAdapter.redeemPositions(bytes32,uint256[])
NEG_RISK_REDEEM_SELECTOR = "0xdbeccb23"
# ERC1155 balanceOf(address,uint256)
ERC1155_BALANCE_OF_SELECTOR = "0x00fdd58e"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

DB_PATH = os.getenv("DB_PATH", "data/trades.db")
GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class OpenPosition:
    trade_id: int
    market_id: str
    token_id: str
    question: str
    direction: str  # "BUY_YES" or "BUY_NO"
    entry_price: float
    size: float


@dataclass
class MarketStatus:
    closed: bool
    uma_status: str
    neg_risk: bool
    payouts: list[int] | None  # [1, 0] or [0, 1] after resolution; None if not set
    outcome_prices: list[float]


def _db_connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def _fetch_open_trades(conn: sqlite3.Connection) -> list[OpenPosition]:
    """Get all filled (open) live trades."""
    c = conn.cursor()
    c.execute("""
        SELECT id, market_id, token_id, question, direction, price, size
        FROM trades
        WHERE status = 'filled'
          AND paper_mode = 0
    """)
    return [
        OpenPosition(trade_id=row[0], market_id=row[1], token_id=row[2],
                     question=row[3] or "", direction=row[4] or "",
                     entry_price=float(row[5] or 0), size=float(row[6] or 0))
        for row in c.fetchall()
    ]


async def _fetch_market(client, condition_id: str) -> MarketStatus | None:
    try:
        r = await client.get(f"{GAMMA_API}/markets", params={"condition_ids": condition_id})
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        m = data[0]
    except Exception as e:
        print(f"  ERROR fetching market {condition_id[:12]}...: {e}")
        return None

    payouts = None
    raw_payouts = m.get("payouts")
    if raw_payouts:
        try:
            import json as _json
            if isinstance(raw_payouts, str):
                payouts = [int(x) for x in _json.loads(raw_payouts)]
            elif isinstance(raw_payouts, list):
                payouts = [int(x) for x in raw_payouts]
        except Exception:
            payouts = None

    outcome_prices = [0.5, 0.5]
    raw_op = m.get("outcomePrices")
    if raw_op:
        try:
            import json as _json
            if isinstance(raw_op, str):
                outcome_prices = [float(x) for x in _json.loads(raw_op)]
            elif isinstance(raw_op, list):
                outcome_prices = [float(x) for x in raw_op]
        except Exception:
            pass

    return MarketStatus(
        closed=bool(m.get("closed", False)),
        uma_status=str(m.get("umaResolutionStatus") or ""),
        neg_risk=bool(m.get("negRisk", False)),
        payouts=payouts,
        outcome_prices=outcome_prices,
    )


def _infer_winning_outcome(status: MarketStatus) -> int | None:
    """Return index of winning outcome: 0=YES-side, 1=NO-side. None if unresolved."""
    if status.payouts and len(status.payouts) >= 2:
        if status.payouts[0] == 1 and status.payouts[1] == 0:
            return 0  # YES won
        if status.payouts[0] == 0 and status.payouts[1] == 1:
            return 1  # NO won
    # Fallback: if market 'closed' and outcomePrices are very lopsided, infer
    if status.closed and status.outcome_prices:
        if status.outcome_prices[0] >= 0.99:
            return 0
        if status.outcome_prices[1] >= 0.99:
            return 1
    return None


def _position_holds_winner(direction: str, winning_outcome: int) -> bool:
    """direction 'BUY_YES' + winning=0 → True; 'BUY_NO' + winning=1 → True."""
    direction = (direction or "").upper()
    if winning_outcome == 0:
        return "BUY_YES" in direction
    if winning_outcome == 1:
        return "BUY_NO" in direction
    return False


def _erc1155_balance(contract: str, owner: str, token_id: str) -> int:
    """Returns ERC-1155 balance as raw uint256."""
    data = (ERC1155_BALANCE_OF_SELECTOR
            + _sa._pad_addr(owner)
            + _sa._pad_uint(int(token_id)))
    return _sa._eth_call(contract, data)


def _selector(sig: str) -> str:
    from eth_utils import keccak
    return "0x" + keccak(text=sig).hex()[:8]


def _condition_resolved(condition_id: str) -> bool:
    cid = condition_id.lower().replace("0x", "").rjust(64, "0")
    data = _selector("payoutDenominator(bytes32)") + cid
    return _sa._eth_call(CTF, data) > 0


def _receipt_usdc_to_wallet(receipt: dict, wallet: str) -> float:
    wallet = wallet.lower()
    total = 0.0
    for item in receipt.get("logs", []) or []:
        if (item.get("address") or "").lower() != USDC.lower():
            continue
        topics = [t.lower() for t in item.get("topics", [])]
        if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
            continue
        to_addr = "0x" + topics[2][-40:]
        if to_addr.lower() == wallet:
            total += int(item.get("data", "0x0"), 16) / 1e6
    return total


def _encode_ctf_redeem(condition_id: str) -> str:
    """Standard CTF.redeemPositions(USDC, 0x0, conditionId, [1,2]).

    Tail-encodes dynamic uint256[] of [1, 2] (both outcomes).
    ABI layout (after selector):
      - address USDC (32 bytes)
      - bytes32 parentCollectionId = 0x0 (32 bytes)
      - bytes32 conditionId (32 bytes)
      - uint256 offset to indexSets array (0x80 = 128)
      - uint256 array length (2)
      - uint256[] elements (1, 2)
    """
    cid = condition_id.lower().replace("0x", "")
    parts = [
        CTF_REDEEM_SELECTOR,
        _sa._pad_addr(USDC),
        "0" * 64,                      # parentCollectionId zero
        cid.rjust(64, "0"),            # conditionId
        _sa._pad_uint(0x80),           # offset to array
        _sa._pad_uint(2),              # array length
        _sa._pad_uint(1),              # indexSet = outcome 0
        _sa._pad_uint(2),              # indexSet = outcome 1
    ]
    return "".join(parts)


def _encode_neg_risk_redeem(condition_id: str, yes_amount: int, no_amount: int) -> str:
    """NegRiskAdapter.redeemPositions(bytes32 conditionId, uint256[] amounts)."""
    cid = condition_id.lower().replace("0x", "")
    return (
        NEG_RISK_REDEEM_SELECTOR
        + cid.rjust(64, "0")
        + _sa._pad_uint(0x40)
        + _sa._pad_uint(2)
        + _sa._pad_uint(yes_amount)
        + _sa._pad_uint(no_amount)
    )


def _neg_risk_amounts(direction: str, raw_balance: int) -> tuple[int, int]:
    direction = (direction or "").upper()
    if "BUY_NO" in direction:
        return 0, raw_balance
    if "BUY_YES" in direction:
        return raw_balance, 0
    raise ValueError(f"cannot infer neg-risk outcome from direction={direction!r}")


def _update_trade_closed(conn: sqlite3.Connection, trade_id: int, payout_usdc: float, cost: float) -> None:
    """Mark trade as closed with realised PnL."""
    pnl = payout_usdc - cost
    c = conn.cursor()
    c.execute(
        "UPDATE trades SET status = 'closed', pnl = ?, closed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (pnl, trade_id),
    )
    conn.commit()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="Actually submit on-chain redemption transactions")
    ap.add_argument("--only-trade-id", type=int, default=None,
                    help="Only process a specific trade ID (debugging)")
    args = ap.parse_args()

    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set")
        return 1
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    from eth_account import Account
    account = Account.from_key(private_key)
    print(f"Wallet: {account.address}")
    print(f"Mode:   {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print()

    conn = _db_connect()
    open_trades = _fetch_open_trades(conn)
    if args.only_trade_id is not None:
        open_trades = [t for t in open_trades if t.trade_id == args.only_trade_id]
    print(f"Found {len(open_trades)} open live trades in DB")

    if not open_trades:
        return 0

    redeemed_count = 0
    loser_count = 0
    skipped_count = 0

    market_counts: dict[str, int] = {}
    for t in open_trades:
        market_counts[t.market_id] = market_counts.get(t.market_id, 0) + 1

    # Group by conditionId so we redeem once per market (both outcomes covered)
    processed_markets: set[str] = set()

    async with httpx.AsyncClient(timeout=30.0) as client:
        market_status_by_id = {
            market_id: await _fetch_market(client, market_id)
            for market_id in market_counts
        }

    for t in open_trades:
        print(f"[{t.trade_id}] {t.question[:60]}")
        status = market_status_by_id.get(t.market_id)
        is_neg_risk = bool(status.neg_risk) if status else False

        if market_counts.get(t.market_id, 0) > 1:
            print("  SKIP: multiple open DB trades share this condition; manual allocation required")
            skipped_count += 1
            continue

        try:
            resolved = _condition_resolved(t.market_id)
        except Exception as e:
            print(f"  ERROR reading on-chain payout state: {e}")
            skipped_count += 1
            continue
        if not resolved:
            print("  SKIP: condition not finalised on-chain yet")
            skipped_count += 1
            continue

        # Check token balance on-chain
        try:
            raw_balance = _erc1155_balance(CTF, account.address, t.token_id)
        except Exception as e:
            print(f"  ERROR reading CTF balance: {e}")
            skipped_count += 1
            continue

        if raw_balance == 0:
            print("  SKIP: CTF balance is 0 (already redeemed or no wallet position)")
            skipped_count += 1
            continue

        shares = raw_balance / 1e6  # CTF tokens are 6-decimal
        print(f"  Holding: {shares:.4f} shares")

        # Dedupe by conditionId (both YES and NO positions share the same market)
        if t.market_id in processed_markets:
            print(f"  SKIP tx: market already redeemed in this run")
            skipped_count += 1
            continue

        # Build + send tx
        if is_neg_risk:
            try:
                yes_amount, no_amount = _neg_risk_amounts(t.direction, raw_balance)
            except ValueError as e:
                print(f"  SKIP: {e}")
                skipped_count += 1
                continue
            data = _encode_neg_risk_redeem(t.market_id, yes_amount, no_amount)
            to = NEG_RISK_ADAPTER
            label = "NegRiskAdapter.redeemPositions"
        else:
            data = _encode_ctf_redeem(t.market_id)
            to = CTF
            label = "CTF.redeemPositions"

        if not args.execute:
            print(f"  DRY-RUN: would call {label} and use receipt USDC delta for PnL")
            continue

        try:
            before = _sa._erc20_balance(USDC, account.address)
            tx_hash = _sa._send_tx(account, to, data, CHAIN_ID)
            receipt = _sa._wait_receipt(tx_hash)
            payout = _receipt_usdc_to_wallet(receipt, account.address)
            if payout <= 0:
                payout = max(0.0, _sa._erc20_balance(USDC, account.address) - before)
            print(f"  ✓ {label}  tx={tx_hash}")
            print(f"  Realised payout from receipt: ${payout:.6f}")
            processed_markets.add(t.market_id)
            _update_trade_closed(conn, t.trade_id, payout_usdc=payout, cost=t.size)
            redeemed_count += 1
            if payout <= 0:
                loser_count += 1
        except Exception as e:
            print(f"  ERROR submitting redeem: {e}")
            skipped_count += 1

    print()
    print(f"=== Summary ===")
    print(f"Redeemed (winners): {redeemed_count}")
    print(f"Losing (marked $0): {loser_count}")
    print(f"Skipped (pending/error): {skipped_count}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
