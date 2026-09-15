#!/usr/bin/env python3
"""Recover wrongly closed live weather winners.

This is intentionally narrow:
- only closed live BUY weather trades with negative PnL;
- only if CLOB `/markets/{conditionId}` says the held token is winner;
- only if wallet still has ERC-1155 balance for that token;
- uses NegRiskAdapter for neg-risk markets, standard CTF otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
import setup_approvals as sa  # noqa: E402

USDC = "0x2791Bca1f2de4661ED88A30C99a7a9449Aa84174"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CHAIN_ID = 137
DB_PATH = os.getenv("DB_PATH", "data/trades.db")

CTF_REDEEM_SELECTOR = "0xb9dd7b0f"
NEG_RISK_REDEEM_SELECTOR = "0xdbeccb23"


@dataclass
class Trade:
    id: int
    market_id: str
    token_id: str
    direction: str
    price: float
    size: float
    pnl: float
    question: str


def _fetch_candidates(conn: sqlite3.Connection) -> list[Trade]:
    rows = conn.execute(
        """
        SELECT id, market_id, token_id, direction, price, size, pnl, question
        FROM trades
        WHERE paper_mode = 0
          AND side = 'BUY'
          AND status = 'closed'
          AND pnl < 0
          AND (
              lower(question) LIKE '%weather%'
              OR lower(question) LIKE '%highest temperature%'
          )
        ORDER BY id
        """
    ).fetchall()
    return [
        Trade(
            id=int(r[0]),
            market_id=str(r[1]),
            token_id=str(r[2]),
            direction=str(r[3]),
            price=float(r[4] or 0),
            size=float(r[5] or 0),
            pnl=float(r[6] or 0),
            question=str(r[7] or ""),
        )
        for r in rows
    ]


def _clob_market(condition_id: str) -> dict | None:
    try:
        req = urllib.request.Request(
            f"https://clob.polymarket.com/markets/{condition_id}",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.load(resp)
    except Exception as exc:
        print(f"  ERROR CLOB market fetch: {exc}")
        return None


def _held_token(market: dict, token_id: str) -> tuple[dict | None, int | None]:
    for idx, token in enumerate(market.get("tokens", []) or []):
        if str(token.get("token_id", "")) == str(token_id):
            return token, idx
    return None, None


def _erc1155_balance(owner: str, token_id: str) -> int:
    data = "0x00fdd58e" + sa._pad_addr(owner) + sa._pad_uint(int(token_id))
    return sa._eth_call(CTF, data)


def _receipt_usdc_to_wallet(receipt: dict, wallet: str) -> float:
    wallet = wallet.lower()
    total = 0.0
    for item in receipt.get("logs", []) or []:
        if (item.get("address") or "").lower() != USDC.lower():
            continue
        topics = [t.lower() for t in item.get("topics", []) or []]
        if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
            continue
        to_addr = "0x" + topics[2][-40:]
        if to_addr.lower() == wallet:
            total += int(item.get("data", "0x0"), 16) / 1e6
    return total


def _encode_ctf_redeem(condition_id: str) -> str:
    cid = condition_id.lower().replace("0x", "").rjust(64, "0")
    return (
        CTF_REDEEM_SELECTOR
        + sa._pad_addr(USDC)
        + "0" * 64
        + cid
        + sa._pad_uint(0x80)
        + sa._pad_uint(2)
        + sa._pad_uint(1)
        + sa._pad_uint(2)
    )


def _encode_neg_risk_redeem(condition_id: str, yes_amount: int, no_amount: int) -> str:
    cid = condition_id.lower().replace("0x", "").rjust(64, "0")
    return (
        NEG_RISK_REDEEM_SELECTOR
        + cid
        + sa._pad_uint(0x40)
        + sa._pad_uint(2)
        + sa._pad_uint(yes_amount)
        + sa._pad_uint(no_amount)
    )


def _neg_risk_amounts(token: dict, token_index: int | None, raw_balance: int) -> tuple[int, int]:
    outcome = str(token.get("outcome", "")).lower()
    if outcome == "yes" or (not outcome and token_index == 0):
        return raw_balance, 0
    if outcome == "no" or (not outcome and token_index == 1):
        return 0, raw_balance
    raise ValueError(f"unknown neg-risk outcome: {token.get('outcome')}")


def _estimate_gas(account, to: str, data: str) -> int:
    tx = {"from": account.address, "to": to, "value": "0x0", "data": data}
    return int(sa._rpc("eth_estimateGas", [tx]), 16)


def _update_trade(conn: sqlite3.Connection, trade_id: int, payout: float, cost: float) -> None:
    pnl = payout - cost
    conn.execute(
        "UPDATE trades SET pnl = ?, order_id = order_id || '|recovered' WHERE id = ?",
        (pnl, trade_id),
    )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--only-trade-id", type=int, default=None)
    args = parser.parse_args()

    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set")
        return 1
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    from eth_account import Account

    account = Account.from_key(private_key)
    conn = sqlite3.connect(DB_PATH)
    candidates = _fetch_candidates(conn)
    if args.only_trade_id is not None:
        candidates = [t for t in candidates if t.id == args.only_trade_id]

    print(f"Wallet: {account.address}")
    print(f"Mode:   {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"Found {len(candidates)} closed negative live weather trades")

    recovered = 0
    skipped = 0
    total_payout = 0.0

    for trade in candidates:
        print(f"\n[{trade.id}] {trade.question[:80]}")
        market = _clob_market(trade.market_id)
        if not market:
            skipped += 1
            continue

        token, token_index = _held_token(market, trade.token_id)
        if token is None:
            print("  SKIP: held token not found in CLOB metadata")
            skipped += 1
            continue

        winner = bool(token.get("winner"))
        price = token.get("price")
        outcome = token.get("outcome")
        neg_risk = bool(market.get("neg_risk"))
        print(f"  CLOB: outcome={outcome} winner={winner} price={price} neg_risk={neg_risk}")
        if not winner:
            print("  SKIP: token is not winner")
            skipped += 1
            continue

        raw_balance = _erc1155_balance(account.address, trade.token_id)
        shares = raw_balance / 1e6
        print(f"  ERC1155 balance: {shares:.6f} shares")
        if raw_balance <= 0:
            print("  SKIP: no token balance left")
            skipped += 1
            continue

        to = NEG_RISK_ADAPTER if neg_risk else CTF
        if neg_risk:
            try:
                yes_amount, no_amount = _neg_risk_amounts(token, token_index, raw_balance)
            except ValueError as exc:
                print(f"  SKIP: {exc}")
                skipped += 1
                continue
            print(f"  Redeem amounts: yes={yes_amount / 1e6:.6f}, no={no_amount / 1e6:.6f}")
            data = _encode_neg_risk_redeem(trade.market_id, yes_amount, no_amount)
        else:
            data = _encode_ctf_redeem(trade.market_id)
        try:
            gas = _estimate_gas(account, to, data)
            print(f"  Gas estimate: {gas}")
        except Exception as exc:
            print(f"  SKIP: gas estimate failed: {exc}")
            skipped += 1
            continue

        expected_payout = shares
        expected_pnl = expected_payout - trade.size
        print(
            f"  Expected payout≈${expected_payout:.6f}, "
            f"DB pnl={trade.pnl:.6f}, corrected pnl≈${expected_pnl:.6f}"
        )

        if not args.execute:
            continue

        before = sa._erc20_balance(USDC, account.address)
        try:
            tx_hash = sa._send_tx(account, to, data, CHAIN_ID)
            receipt = sa._wait_receipt(tx_hash)
        except Exception as exc:
            print(f"  ERROR tx failed: {exc}")
            skipped += 1
            continue

        payout = _receipt_usdc_to_wallet(receipt, account.address)
        if payout <= 0:
            payout = max(0.0, sa._erc20_balance(USDC, account.address) - before)

        print(f"  OK tx={tx_hash}")
        print(f"  Realised payout: ${payout:.6f}")
        _update_trade(conn, trade.id, payout, trade.size)
        recovered += 1
        total_payout += payout

    print("\n=== Summary ===")
    print(f"Recovered: {recovered}")
    print(f"Skipped:   {skipped}")
    print(f"Payout:    ${total_payout:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
