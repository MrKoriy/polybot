#!/usr/bin/env python3
"""One-time on-chain approvals for Polymarket live trading.

Approves USDC.e and CTF outcome-token transfers for the Polymarket Exchange,
Neg-Risk Exchange, and NegRiskAdapter contracts. This requires POL/MATIC gas on
Polygon.

Usage:
    .venv/bin/python scripts/setup_approvals.py
    .venv/bin/python scripts/setup_approvals.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

MAX_UINT256 = 2**256 - 1
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
RPC_FALLBACKS = [
    os.getenv("POLYGON_RPC_URL", "").strip(),
    "https://polygon.drpc.org",
    "https://polygon.rpc.subquery.network/public",
    "https://polygon.api.onfinality.io/public",
]


def _rpc(method: str, params: list) -> str:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    body = json.dumps(payload).encode()
    last_error: Exception | None = None
    for url in [u for u in RPC_FALLBACKS if u]:
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            response = json.load(urllib.request.urlopen(req, timeout=25))
            if "error" in response:
                raise RuntimeError(response["error"])
            return response["result"]
        except Exception as e:
            last_error = e
            continue
    raise RuntimeError(f"all Polygon RPC endpoints failed: {last_error}")


def _pad_addr(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


def _pad_uint(value: int) -> str:
    return hex(value)[2:].rjust(64, "0")


def _eth_call(to: str, data: str) -> int:
    result = _rpc("eth_call", [{"to": to, "data": data}, "latest"])
    return int(result, 16)


def _wait_receipt(tx_hash: str, timeout_s: int = 120) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = _rpc("eth_getTransactionReceipt", [tx_hash])
        if result:
            return result
        time.sleep(3)
    raise TimeoutError(f"timed out waiting for tx receipt: {tx_hash}")


def _send_tx(account, to: str, data: str, chain_id: int) -> str:
    nonce = int(_rpc("eth_getTransactionCount", [account.address, "pending"]), 16)
    gas_price = int(_rpc("eth_gasPrice", []), 16)
    tx_for_estimate = {
        "from": account.address,
        "to": to,
        "value": "0x0",
        "data": data,
    }
    gas_estimate = int(_rpc("eth_estimateGas", [tx_for_estimate]), 16)
    tx = {
        "chainId": chain_id,
        "nonce": nonce,
        "to": to,
        "value": 0,
        "data": data,
        "gas": int(gas_estimate * 1.35),
        "gasPrice": gas_price,
    }
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    tx_hash = _rpc("eth_sendRawTransaction", ["0x" + raw.hex()])
    receipt = _wait_receipt(tx_hash)
    if int(receipt.get("status", "0x0"), 16) != 1:
        raise RuntimeError(f"transaction failed: {tx_hash}")
    return tx_hash


def _erc20_balance(token: str, owner: str) -> float:
    raw = _eth_call(token, "0x70a08231" + _pad_addr(owner))
    return raw / 1e6


def _erc20_allowance(token: str, owner: str, spender: str) -> float:
    raw = _eth_call(token, "0xdd62ed3e" + _pad_addr(owner) + _pad_addr(spender))
    return raw / 1e6


def _erc20_approve_data(spender: str, amount: int = MAX_UINT256) -> str:
    return "0x095ea7b3" + _pad_addr(spender) + _pad_uint(amount)


def _is_approved_for_all(ctf: str, owner: str, operator: str) -> bool:
    raw = _eth_call(ctf, "0xe985e9c5" + _pad_addr(owner) + _pad_addr(operator))
    return raw != 0


def _set_approval_for_all_data(operator: str, approved: bool = True) -> str:
    return "0xa22cb465" + _pad_addr(operator) + _pad_uint(1 if approved else 0)


def _refresh_clob_cache(client, signature_type: int) -> None:
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    client.update_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket one-time approvals setup")
    parser.add_argument("--dry-run", action="store_true", help="Read current state only")
    args = parser.parse_args()

    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY is not set.")
        return 1

    clob_url = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
    chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS", "").strip() or None

    try:
        from eth_account import Account
        from py_clob_client.client import ClobClient
        from py_clob_client.config import get_contract_config
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
    except ImportError as e:
        print(f"ERROR: missing dependency: {e}")
        return 1

    account = Account.from_key(private_key)
    config = get_contract_config(chain_id)
    neg_config = get_contract_config(chain_id, neg_risk=True)
    print(f"Wallet: {account.address}")
    print(f"CLOB:   {clob_url} (chain {chain_id}, signature_type={signature_type})")

    kwargs = {
        "host": clob_url,
        "chain_id": chain_id,
        "key": private_key,
        "signature_type": signature_type,
    }
    if signature_type in (1, 2) and funder:
        kwargs["funder"] = funder
    client = ClobClient(**kwargs)
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)

    c_bal = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type)
    )
    clob_balance = float(c_bal.get("balance", 0)) / 1e6
    clob_allowance = float(c_bal.get("allowance", 0)) / 1e6
    native_pol = int(_rpc("eth_getBalance", [account.address, "latest"]), 16) / 1e18
    onchain_usdc = _erc20_balance(config.collateral, account.address)
    allowance_exchange = _erc20_allowance(config.collateral, account.address, config.exchange)
    allowance_neg = _erc20_allowance(config.collateral, account.address, neg_config.exchange)
    ctf_exchange = _is_approved_for_all(config.conditional_tokens, account.address, config.exchange)
    ctf_neg = _is_approved_for_all(config.conditional_tokens, account.address, neg_config.exchange)
    ctf_neg_adapter = _is_approved_for_all(config.conditional_tokens, account.address, NEG_RISK_ADAPTER)

    print(f"  POL gas balance:      {native_pol:.8f}")
    print(f"  USDC.e on-chain:      ${onchain_usdc:,.4f}")
    print(f"  CLOB USDC balance:    ${clob_balance:,.4f}")
    print(f"  CLOB USDC allowance:  ${clob_allowance:,.2f}")
    print(f"  Exchange allowance:   ${allowance_exchange:,.2f}")
    print(f"  Neg-risk allowance:   ${allowance_neg:,.2f}")
    print(f"  CTF exchange approval: {ctf_exchange}")
    print(f"  CTF neg-risk approval: {ctf_neg}")
    print(f"  CTF neg-risk adapter approval: {ctf_neg_adapter}")

    if args.dry_run:
        print("\nDRY RUN - not submitting transactions.")
        return 0

    if native_pol <= 0:
        print("\nERROR: wallet has 0 POL/MATIC. Send POL on Polygon for gas, then rerun.")
        return 1

    targets = [
        ("USDC.e Exchange", config.collateral, _erc20_approve_data(config.exchange), allowance_exchange),
        ("USDC.e Neg-risk Exchange", config.collateral, _erc20_approve_data(neg_config.exchange), allowance_neg),
    ]
    for label, contract, data, current_allowance in targets:
        if current_allowance >= 10000:
            print(f"\n{label}: allowance already sufficient.")
            continue
        print(f"\nSubmitting {label} approval...")
        tx_hash = _send_tx(account, contract, data, chain_id)
        print(f"  confirmed: {tx_hash}")

    ctf_targets = [
        ("CTF Exchange", config.conditional_tokens, _set_approval_for_all_data(config.exchange), ctf_exchange),
        ("CTF Neg-risk Exchange", config.conditional_tokens, _set_approval_for_all_data(neg_config.exchange), ctf_neg),
        (
            "CTF Neg-risk Adapter",
            config.conditional_tokens,
            _set_approval_for_all_data(NEG_RISK_ADAPTER),
            ctf_neg_adapter,
        ),
    ]
    for label, contract, data, already_approved in ctf_targets:
        if already_approved:
            print(f"\n{label}: already approved.")
            continue
        print(f"\nSubmitting {label} approval...")
        tx_hash = _send_tx(account, contract, data, chain_id)
        print(f"  confirmed: {tx_hash}")

    print("\nRefreshing CLOB balance/allowance cache...")
    _refresh_clob_cache(client, signature_type)
    time.sleep(3)
    c_bal = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type)
    )
    print(f"  CLOB USDC allowance now: ${float(c_bal.get('allowance', 0)) / 1e6:,.2f}")
    print("\nDONE. Run scripts/check_live_ready.py to verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
