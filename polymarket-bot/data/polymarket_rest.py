import asyncio
import json
import time
from datetime import datetime, timedelta

import httpx
import structlog

from config.constants import GAMMA_API_URL
from core.exceptions import PolymarketAPIError
from data.base import MarketCandidate

log = structlog.get_logger()

_POLYGON_RPC_FALLBACKS = [
    "https://polygon.drpc.org",
    "https://polygon.rpc.subquery.network/public",
    "https://polygon.api.onfinality.io/public",
]
_USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_CTF_POLYGON = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_ZERO32 = "0" * 64


class PolymarketClient:
    def __init__(
        self,
        private_key: str = "",
        chain_id: int = 137,
        clob_url: str = "https://clob.polymarket.com",
        gamma_url: str = GAMMA_API_URL,
        clob_proxy: str = "",
        signature_type: int = 0,
        funder_address: str = "",
        polygon_rpc_url: str = "",
    ) -> None:
        self._private_key = private_key
        self._chain_id = chain_id
        self._clob_url = clob_url
        self._gamma_url = gamma_url
        self._clob_proxy = clob_proxy  # SOCKS5 proxy for CLOB (EU IP for non-KYC)
        self._signature_type = int(signature_type)
        self._funder_address = funder_address
        self._polygon_rpc_url = polygon_rpc_url or "https://polygon-rpc.com"
        self._clob_client = None
        self._http = httpx.AsyncClient(timeout=60.0)

    async def init_clob(self) -> None:
        if not self._private_key:
            log.warning("clob_skip_init", reason="no private key (paper mode only)")
            return

        try:
            from py_clob_client.client import ClobClient

            proxy = self._clob_proxy
            sig_type = self._signature_type
            funder = self._funder_address

            def _init():
                # py_clob_client uses `requests` internally — inject proxy via session
                kwargs = dict(
                    host=self._clob_url,
                    key=self._private_key,
                    chain_id=self._chain_id,
                    signature_type=sig_type,
                )
                # For POLY_PROXY (1) or POLY_GNOSIS_SAFE (2), pass the funder address
                # (i.e. the proxy/safe contract address that actually holds funds).
                if sig_type in (1, 2) and funder:
                    kwargs["funder"] = funder
                client = ClobClient(**kwargs)
                creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)

                # Patch requests session to route through SOCKS5 proxy (EU IP)
                if proxy:
                    import requests
                    proxies = {"http": proxy, "https": proxy}
                    if hasattr(client, "session"):
                        client.session.proxies.update(proxies)
                    elif hasattr(client, "_session"):
                        client._session.proxies.update(proxies)
                    log.info("clob_proxy_configured", proxy=proxy.split("@")[-1])

                return client

            self._clob_client = await asyncio.to_thread(_init)
            sig_name = {0: "EOA", 1: "POLY_PROXY", 2: "POLY_GNOSIS_SAFE"}.get(
                self._signature_type, f"type_{self._signature_type}"
            )
            log.info("clob_initialized", signature_type=sig_name,
                     funder=self._funder_address or "auto")
        except Exception as e:
            log.error("clob_init_error", error=str(e))
            raise PolymarketAPIError(f"Failed to init CLOB client: {e}") from e

    async def get_active_markets(
        self,
        min_volume: float = 1000,
        max_days_to_resolution: int = 3,
        limit: int = 200,
    ) -> list[MarketCandidate]:
        try:
            # Use Gamma API for market discovery
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            }
            resp = await self._http.get(f"{self._gamma_url}/markets", params=params)
            resp.raise_for_status()
            raw_markets = resp.json()

            candidates = []
            now = datetime.utcnow()
            max_end = now + timedelta(days=max_days_to_resolution)

            for m in raw_markets:
                try:
                    end_date_str = m.get("endDate") or m.get("end_date_iso", "")
                    if not end_date_str:
                        continue

                    # Parse end date
                    end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    if end_date.replace(tzinfo=None) > max_end:
                        continue

                    volume = float(m.get("volumeNum", 0) or m.get("volume", 0) or 0)
                    volume_24h = float(m.get("volume24hr", 0) or 0)

                    if volume < min_volume:
                        continue

                    # Get token IDs — Gamma API returns clobTokenIds as JSON string
                    raw_tokens = m.get("tokens") or m.get("clobTokenIds")
                    if not raw_tokens:
                        continue

                    # Parse tokens: can be a JSON string or a list
                    if isinstance(raw_tokens, str):
                        token_list = json.loads(raw_tokens)
                    elif isinstance(raw_tokens, list):
                        token_list = raw_tokens
                    else:
                        continue

                    if not token_list:
                        continue

                    # Parse outcome prices — also a JSON string from Gamma
                    raw_prices = m.get("outcomePrices", "[0.5, 0.5]")
                    if isinstance(raw_prices, str):
                        price_list = json.loads(raw_prices)
                    elif isinstance(raw_prices, list):
                        price_list = raw_prices
                    else:
                        price_list = [0.5, 0.5]

                    if isinstance(token_list[0], dict):
                        # Structured tokens with outcome info
                        yes_token = next(
                            (t["token_id"] for t in token_list if t.get("outcome", "").upper() == "YES"),
                            token_list[0].get("token_id", ""),
                        )
                        no_token = next(
                            (t["token_id"] for t in token_list if t.get("outcome", "").upper() == "NO"),
                            token_list[1].get("token_id", "") if len(token_list) > 1 else "",
                        )
                        yes_price = float(next(
                            (t.get("price", 0.5) for t in token_list if t.get("outcome", "").upper() == "YES"),
                            0.5,
                        ))
                        no_price = float(next(
                            (t.get("price", 0.5) for t in token_list if t.get("outcome", "").upper() == "NO"),
                            0.5,
                        ))
                    else:
                        # Simple token ID list + separate prices
                        yes_token = str(token_list[0])
                        no_token = str(token_list[1]) if len(token_list) > 1 else ""
                        yes_price = float(price_list[0])
                        no_price = float(price_list[1]) if len(price_list) > 1 else 1.0 - yes_price

                    spread = abs(1.0 - yes_price - no_price)

                    candidate = MarketCandidate(
                        condition_id=m.get("conditionId", m.get("condition_id", "")),
                        question=m.get("question", ""),
                        yes_token_id=yes_token,
                        no_token_id=no_token,
                        yes_price=yes_price,
                        no_price=no_price,
                        volume=volume,
                        volume_24h=volume_24h,
                        end_date=end_date_str,
                        spread=spread,
                        category=m.get("category", ""),
                        tags=m.get("tags", []) or [],
                    )
                    candidates.append(candidate)

                except (KeyError, ValueError, IndexError) as e:
                    log.debug("market_parse_skip", error=str(e), market_id=m.get("conditionId"))
                    continue

            log.info("markets_fetched", total=len(raw_markets), candidates=len(candidates))
            return candidates

        except httpx.HTTPError as e:
            raise PolymarketAPIError(f"Gamma API error: {e}") from e

    async def gamma_health_check(self) -> bool:
        """Probe Gamma reachability without applying trading filters."""
        try:
            resp = await self._http.get(
                f"{self._gamma_url}/markets",
                params={"closed": "false", "limit": 1},
            )
            resp.raise_for_status()
            payload = resp.json()
            return isinstance(payload, list)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("gamma_health_check_failed", error=str(e))
            return False

    async def get_orderbook(self, token_id: str) -> dict:
        """Return {"bids": [...], "asks": [...]} for a token. 2-second cache."""
        # H5: 2-second in-memory cache. Prevents rate-limit during FOK bursts.
        import time as _t
        if not hasattr(self, "_book_cache"):
            self._book_cache: dict[str, tuple[dict, float]] = {}
        now = _t.monotonic()
        cached = self._book_cache.get(token_id)
        if cached and (now - cached[1]) < 2.0:
            return cached[0]

        def _to_dict(levels):
            out = []
            for lvl in (levels or []):
                if isinstance(lvl, dict):
                    out.append(lvl)
                else:
                    out.append({
                        "price": getattr(lvl, "price", ""),
                        "size": getattr(lvl, "size", ""),
                    })
            return out

        if self._clob_client:
            try:
                raw = await asyncio.to_thread(self._clob_client.get_order_book, token_id)
                # py_clob_client returns an OrderBookSummary object; normalise to dict
                if hasattr(raw, "bids") or hasattr(raw, "asks"):
                    book = {
                        "bids": _to_dict(getattr(raw, "bids", [])),
                        "asks": _to_dict(getattr(raw, "asks", [])),
                        "tick_size": getattr(raw, "tick_size", None),
                        "min_order_size": getattr(raw, "min_order_size", None),
                    }
                else:
                    book = raw if isinstance(raw, dict) else {"bids": [], "asks": []}
                self._book_cache[token_id] = (book, now)
                return book
            except Exception as e:
                log.debug("orderbook_clob_error", token_id=token_id, error=str(e))

        # Public CLOB REST endpoint — required for realistic paper mode where no
        # authenticated CLOB client is initialised.
        try:
            resp = await self._http.get(
                f"{self._clob_url}/book",
                params={"token_id": token_id},
            )
            if resp.status_code == 200:
                raw = resp.json()
                book = {
                    "bids": _to_dict(raw.get("bids", [])),
                    "asks": _to_dict(raw.get("asks", [])),
                    "tick_size": raw.get("tick_size"),
                    "min_order_size": raw.get("min_order_size"),
                    "neg_risk": raw.get("neg_risk"),
                }
                self._book_cache[token_id] = (book, now)
                return book
            log.debug("orderbook_rest_non_200", token_id=token_id, status=resp.status_code)
        except Exception as e:
            log.debug("orderbook_rest_error", token_id=token_id, error=str(e))
        return {"bids": [], "asks": []}

    async def get_fee_rate(self, token_id: str) -> float | None:
        """Dynamic taker fee rate (fraction) for a token via public CLOB /fee-rate.

        py_clob_client signs live orders with this fee automatically, so the
        bot's edge/PnL math MUST account for it — otherwise paper (fee-free)
        and live (fee-bearing) systematically diverge. Cached 5 min.
        Returns None when unavailable (caller decides fallback).
        """
        import time as _t
        if not hasattr(self, "_fee_cache"):
            self._fee_cache: dict[str, tuple[float, float]] = {}
        now = _t.monotonic()
        cached = self._fee_cache.get(token_id)
        if cached and (now - cached[1]) < 300.0:
            return cached[0]

        try:
            resp = await self._http.get(
                f"{self._clob_url}/fee-rate",
                params={"tokenID": token_id},
            )
            if resp.status_code == 200:
                data = resp.json()
                # Response carries base_fee in basis points.
                raw = data.get("base_fee", data.get("feeRate", data.get("fee_rate")))
                if raw is not None:
                    rate = float(raw) / 10_000.0
                    self._fee_cache[token_id] = (rate, now)
                    return rate
        except Exception as e:
            log.debug("fee_rate_error", token_id=token_id, error=str(e))
        return None

    async def get_midpoint(self, token_id: str) -> float | None:
        # Try CLOB client first (authenticated), then fall back to public REST endpoint
        if self._clob_client:
            try:
                mid = await asyncio.to_thread(self._clob_client.get_midpoint, token_id)
                if isinstance(mid, dict):
                    mid = mid.get("mid")
                if mid:
                    return float(mid)
            except Exception as e:
                log.debug("midpoint_clob_error", token_id=token_id, error=str(e))

        # Public CLOB REST endpoint — works without auth, perfect for paper mode
        try:
            resp = await self._http.get(
                f"{self._clob_url}/midpoint",
                params={"token_id": token_id},
            )
            if resp.status_code == 200:
                data = resp.json()
                mid = data.get("mid")
                if mid:
                    return float(mid)
        except Exception as e:
            log.debug("midpoint_rest_error", token_id=token_id, error=str(e))
        return None

    async def get_clob_market(self, condition_id: str) -> dict | None:
        """Fetch CLOB market metadata by condition id.

        Gamma filtering is not reliable for condition_id lookups on closed
        markets. The CLOB `/markets/{condition_id}` endpoint returns resolved
        token winner flags, which are the safest source for live settlement.
        """
        try:
            resp = await self._http.get(f"{self._clob_url}/markets/{condition_id}")
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            log.debug("clob_market_fetch_error", condition_id=condition_id, error=str(e))
            return None

    def _rpc_urls(self) -> list[str]:
        urls = [self._polygon_rpc_url, *_POLYGON_RPC_FALLBACKS]
        return list(dict.fromkeys([u for u in urls if u]))

    async def _polygon_rpc(self, method: str, params: list) -> str:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        last_error: Exception | None = None
        for url in self._rpc_urls():
            try:
                resp = await self._http.post(
                    url,
                    json=payload,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data["result"]
            except Exception as e:
                last_error = e
        raise PolymarketAPIError(f"Polygon RPC failed: {last_error}")

    @staticmethod
    def _pad_addr(addr: str) -> str:
        return addr.lower().replace("0x", "").rjust(64, "0")

    @staticmethod
    def _pad_bytes32(value: str) -> str:
        return value.lower().replace("0x", "").rjust(64, "0")

    @staticmethod
    def _pad_uint(value: int | str) -> str:
        return hex(int(value))[2:].rjust(64, "0")

    @staticmethod
    def _selector(signature: str) -> str:
        from eth_utils import keccak
        return keccak(text=signature).hex()[:8]

    async def _eth_call_uint(self, to: str, data: str) -> int:
        result = await self._polygon_rpc("eth_call", [{"to": to, "data": data}, "latest"])
        return int(result, 16)

    async def _erc20_balance_rpc(self, token: str, owner: str) -> int:
        data = "0x70a08231" + self._pad_addr(owner)
        return await self._eth_call_uint(token, data)

    async def _erc1155_balance_rpc(self, owner: str, token_id: str) -> int:
        data = "0x00fdd58e" + self._pad_addr(owner) + self._pad_uint(token_id)
        return await self._eth_call_uint(_CTF_POLYGON, data)

    async def get_condition_payouts(self, condition_id: str) -> dict:
        """Return official CTF payout state for a binary condition.

        `denominator == 0` means UMA/CTF has not finalised yet. Once finalised,
        redeeming the held CTF tokens is the reliable close path; the CLOB book
        may already be gone.
        """
        denom_data = (
            "0x"
            + self._selector("payoutDenominator(bytes32)")
            + self._pad_bytes32(condition_id)
        )
        denominator = await self._eth_call_uint(_CTF_POLYGON, denom_data)

        numerators: list[int] = []
        for idx in (0, 1):
            num_data = (
                "0x"
                + self._selector("payoutNumerators(bytes32,uint256)")
                + self._pad_bytes32(condition_id)
                + self._pad_uint(idx)
            )
            numerators.append(await self._eth_call_uint(_CTF_POLYGON, num_data))

        return {
            "resolved": denominator > 0,
            "denominator": denominator,
            "numerators": numerators,
        }

    async def get_conditional_balance(self, token_id: str) -> float:
        """Return CTF outcome-token balance for this wallet in share units."""
        balance = await self._get_conditional_balance_clob(token_id)
        # The CLOB balance endpoint can return stale zero for live CTF tokens.
        # Treat zero as inconclusive and verify via ERC-1155 balanceOf on Polygon.
        if balance is None or balance <= 0:
            balance = await self._get_conditional_balance_rpc(token_id)
        return float(balance or 0.0)

    @staticmethod
    def _redeem_positions_data(condition_id: str) -> str:
        return (
            "0x"
            + PolymarketClient._selector("redeemPositions(address,bytes32,bytes32,uint256[])")
            + PolymarketClient._pad_addr(_USDC_POLYGON)
            + _ZERO32
            + PolymarketClient._pad_bytes32(condition_id)
            + PolymarketClient._pad_uint(128)
            + PolymarketClient._pad_uint(2)
            + PolymarketClient._pad_uint(1)
            + PolymarketClient._pad_uint(2)
        )

    @staticmethod
    def _neg_risk_redeem_positions_data(condition_id: str, yes_amount: int, no_amount: int) -> str:
        return (
            "0x"
            + PolymarketClient._selector("redeemPositions(bytes32,uint256[])")
            + PolymarketClient._pad_bytes32(condition_id)
            + PolymarketClient._pad_uint(64)
            + PolymarketClient._pad_uint(2)
            + PolymarketClient._pad_uint(yes_amount)
            + PolymarketClient._pad_uint(no_amount)
        )

    async def _wait_receipt(self, tx_hash: str, timeout: int = 180) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            receipt = await self._polygon_rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                status = int(receipt.get("status", "0x0"), 16)
                if status != 1:
                    raise PolymarketAPIError(f"Polygon tx failed: {tx_hash}")
                return receipt
            await asyncio.sleep(3)
        raise PolymarketAPIError(f"Polygon tx receipt timeout: {tx_hash}")

    async def _send_polygon_tx(self, account, to: str, data: str) -> tuple[str, dict]:
        nonce = int(
            await self._polygon_rpc("eth_getTransactionCount", [account.address, "pending"]),
            16,
        )
        gas_price = int(await self._polygon_rpc("eth_gasPrice", []), 16)
        estimate = {
            "from": account.address,
            "to": to,
            "value": "0x0",
            "data": data,
        }
        gas_estimate = int(await self._polygon_rpc("eth_estimateGas", [estimate]), 16)
        tx = {
            "chainId": self._chain_id,
            "nonce": nonce,
            "to": to,
            "value": 0,
            "data": data,
            "gas": int(gas_estimate * 1.35),
            "gasPrice": gas_price,
        }
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        raw_hex = raw.hex()
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        tx_hash = await self._polygon_rpc("eth_sendRawTransaction", [raw_hex])
        receipt = await self._wait_receipt(tx_hash)
        return tx_hash, receipt

    @staticmethod
    def _receipt_usdc_to_wallet(receipt: dict, wallet: str) -> float:
        wallet = wallet.lower()
        total = 0.0
        for item in receipt.get("logs", []) or []:
            if (item.get("address") or "").lower() != _USDC_POLYGON.lower():
                continue
            topics = [t.lower() for t in (item.get("topics") or [])]
            if len(topics) < 3 or topics[0] != _TRANSFER_TOPIC:
                continue
            to_addr = "0x" + topics[2][-40:]
            if to_addr.lower() == wallet:
                total += int(item.get("data", "0x0"), 16) / 1e6
        return total

    async def redeem_resolved_position(self, condition_id: str, token_id: str) -> dict:
        """Redeem a resolved binary market and return the realised USDC payout.

        This intentionally uses the transaction receipt, not local YES/NO labels,
        because Gamma/CLOB outcome ordering can differ and stale metadata can
        otherwise create fake live PnL.
        """
        if not self._private_key:
            return {"success": False, "error": "missing_private_key"}

        payouts = await self.get_condition_payouts(condition_id)
        if not payouts["resolved"]:
            return {"success": False, "error": "condition_not_resolved", **payouts}

        try:
            from eth_account import Account
        except Exception as e:
            return {"success": False, "error": f"eth_account_missing: {e}", **payouts}

        account = Account.from_key(self._private_key)
        owner = self._funder_address or account.address
        if owner.lower() != account.address.lower():
            return {
                "success": False,
                "error": "redeem_proxy_wallet_unsupported",
                "owner": owner,
                **payouts,
            }

        raw_token_balance = await self._erc1155_balance_rpc(account.address, token_id)
        shares = raw_token_balance / 1e6
        if raw_token_balance <= 0:
            return {
                "success": False,
                "error": "zero_token_balance",
                "shares": 0.0,
                **payouts,
            }

        market_meta = await self.get_clob_market(condition_id)
        token_meta = None
        token_index = None
        market_tokens = (market_meta or {}).get("tokens", []) or []
        for idx, token in enumerate(market_tokens):
            if str(token.get("token_id", "")) == str(token_id):
                token_meta = token
                token_index = idx
                break

        # Avoid submitting pointless redeem txs for known losing tokens. More
        # importantly, neg-risk markets must redeem through the adapter; calling
        # raw CTF.redeemPositions can return $0 even for CLOB winner tokens.
        if token_meta is not None and token_meta.get("winner") is False:
            return {
                "success": True,
                "tx_hash": "",
                "payout": 0.0,
                "shares": shares,
                "known_loser": True,
                **payouts,
            }

        before_usdc = await self._erc20_balance_rpc(_USDC_POLYGON, account.address)
        is_neg_risk = bool((market_meta or {}).get("neg_risk"))
        redeem_data = self._redeem_positions_data(condition_id)
        if is_neg_risk:
            if token_meta is None:
                return {
                    "success": False,
                    "error": "missing_neg_risk_token_meta",
                    "shares": shares,
                    **payouts,
                }
            outcome = str(token_meta.get("outcome", "")).lower()
            if outcome == "yes" or (not outcome and token_index == 0):
                yes_amount, no_amount = raw_token_balance, 0
            elif outcome == "no" or (not outcome and token_index == 1):
                yes_amount, no_amount = 0, raw_token_balance
            else:
                return {
                    "success": False,
                    "error": f"unknown_neg_risk_outcome:{token_meta.get('outcome')}",
                    "shares": shares,
                    **payouts,
                }
            redeem_data = self._neg_risk_redeem_positions_data(
                condition_id,
                yes_amount,
                no_amount,
            )

        tx_hash, receipt = await self._send_polygon_tx(
            account,
            _NEG_RISK_ADAPTER if is_neg_risk else _CTF_POLYGON,
            redeem_data,
        )
        payout = self._receipt_usdc_to_wallet(receipt, account.address)
        if payout <= 0:
            after_usdc = await self._erc20_balance_rpc(_USDC_POLYGON, account.address)
            payout = max(0.0, (after_usdc - before_usdc) / 1e6)

        return {
            "success": True,
            "tx_hash": tx_hash,
            "payout": payout,
            "shares": shares,
            "neg_risk": is_neg_risk,
            **payouts,
        }

    async def get_market_resolution(self, condition_id: str) -> dict:
        """Return the ACTUAL resolution state of a market.

        Returns:
          {"resolved": bool, "yes_won": bool | None}

        `yes_won` refers to outcome index 0 (the "Yes"/"Up" token, i.e.
        clobTokenIds[0]). Never invents an outcome — unresolved markets
        return resolved=False and the caller must keep holding.

        SOURCE: CLOB /markets/{condition_id} is PRIMARY — its token winner
        flags are the settlement truth. The Gamma ?conditionId= query returns
        STALE data for closed markets (verified Sep 2026: closed market showed
        prices 0.0485/0.9515 there while the true outcome was 0/1). Gamma
        outcomePrices are only a fallback.
        """
        # 1. CLOB market metadata — authoritative winner flags.
        clob = await self.get_clob_market(condition_id)
        if clob:
            tokens = clob.get("tokens") or []
            if clob.get("closed") and len(tokens) >= 2:
                winners = [t for t in tokens if t.get("winner")]
                if len(winners) == 1:
                    yes_won = (
                        str(winners[0].get("token_id") or "")
                        == str(tokens[0].get("token_id") or "")
                    )
                    return {"resolved": True, "yes_won": yes_won}
                # closed but no/ambiguous winner → not settled yet
                return {"resolved": False, "yes_won": None}

        # 2. Gamma fallback: resolved flag + final outcomePrices.
        try:
            resp = await self._http.get(
                f"{self._gamma_url}/markets",
                params={"conditionId": condition_id, "limit": 1},
            )
            resp.raise_for_status()
            markets = resp.json()
            if not markets:
                return {"resolved": False, "yes_won": None}
            data = markets[0] if isinstance(markets, list) else markets
            if data.get("conditionId") != condition_id:
                return {"resolved": False, "yes_won": None}

            resolved = bool(data.get("resolved", False))
            raw_prices = data.get("outcomePrices", "")
            if isinstance(raw_prices, str) and raw_prices:
                price_list = json.loads(raw_prices)
            elif isinstance(raw_prices, list):
                price_list = raw_prices
            else:
                price_list = []

            if price_list:
                try:
                    p0 = float(price_list[0])
                    if p0 in (0.0, 1.0):
                        return {"resolved": True, "yes_won": p0 == 1.0}
                except (TypeError, ValueError):
                    pass

            outcome = (data.get("outcome", "") or "").upper()
            if resolved and outcome == "YES":
                return {"resolved": True, "yes_won": True}
            if resolved and outcome == "NO":
                return {"resolved": True, "yes_won": False}

            return {"resolved": False, "yes_won": None}
        except Exception as e:
            log.debug("gamma_resolution_error", condition_id=condition_id[:20], error=str(e))
            return {"resolved": False, "yes_won": None}

    async def get_market_price_gamma(self, condition_id: str) -> float | None:
        """Look up a market's current YES price via the Gamma API (no auth needed).

        Works in both paper and live mode. Returns None if the market is not
        found or the API is unreachable.
        """
        try:
            # Gamma API /markets/{id} expects numeric ID, not conditionId.
            # Use query param search with exact conditionId instead.
            resp = await self._http.get(
                f"{self._gamma_url}/markets",
                params={"conditionId": condition_id, "limit": 1},
            )
            resp.raise_for_status()
            markets = resp.json()

            if not markets:
                return None

            data = markets[0] if isinstance(markets, list) else markets

            # Verify we got the right market (conditionId query does substring match)
            if data.get("conditionId") != condition_id:
                return None

            # Check if market has resolved
            resolved = data.get("resolved", False)
            if resolved:
                outcome = (data.get("outcome", "") or "").upper()
                if outcome == "YES":
                    return 1.0
                elif outcome == "NO":
                    return 0.0

            # Get current price from outcomePrices
            raw_prices = data.get("outcomePrices", "")
            if isinstance(raw_prices, str) and raw_prices:
                price_list = json.loads(raw_prices)
            elif isinstance(raw_prices, list):
                price_list = raw_prices
            else:
                return None

            if price_list:
                return float(price_list[0])
            return None
        except Exception as e:
            log.debug("gamma_price_error", condition_id=condition_id[:20], error=str(e))
            return None

    async def get_balance(self) -> float:
        """Return USDC collateral balance in USDC (6-decimal units → float USDC)."""
        if not self._clob_client:
            return 0.0
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=self._signature_type)
            result = await asyncio.to_thread(
                self._clob_client.get_balance_allowance, params
            )
            # py_clob_client returns dict: {"balance": "N_usdc_wei", "allowance": "..."}
            raw = result.get("balance", 0) if isinstance(result, dict) else 0
            return float(raw) / 1e6
        except Exception as e:
            log.error("balance_error", error=str(e))
            return 0.0

    async def get_allowance(self) -> float:
        """Return current USDC allowance to Exchange in USDC.

        CLOB response shape: {"balance": "...", "allowances": {spender: amount_str}}.
        Takes the MAX across spenders — orders route to whichever spender has approval.
        """
        if not self._clob_client:
            return 0.0
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=self._signature_type)
            result = await asyncio.to_thread(
                self._clob_client.get_balance_allowance, params
            )
            if not isinstance(result, dict):
                return 0.0
            allowances = result.get("allowances") or {}
            if isinstance(allowances, dict) and allowances:
                max_raw = max((int(v) for v in allowances.values() if v is not None), default=0)
                return min(float(max_raw) / 1e6, 1e12)
            raw = result.get("allowance", 0)
            return float(raw) / 1e6
        except Exception as e:
            log.error("allowance_error", error=str(e))
            return 0.0

    async def get_tick_size(self, token_id: str) -> float:
        """Get minimum tick size for a token. Defaults to 0.01 if unavailable."""
        if not self._clob_client:
            return 0.01
        try:
            tick = await asyncio.to_thread(self._clob_client.get_tick_size, token_id)
            return float(tick) if tick else 0.01
        except Exception as e:
            log.debug("tick_size_error", token_id=token_id, error=str(e))
            return 0.01

    async def place_fok_order(
        self, token_id: str, side: str, price: float, size: float
    ) -> dict:
        """Place a Fill-or-Kill order — fills immediately at price or cancels. No heartbeat needed."""
        if not self._clob_client:
            raise PolymarketAPIError("CLOB client not initialized")

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            order_side = BUY if side.upper() == "BUY" else SELL
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=order_side,
            )

            def _place():
                signed_order = self._clob_client.create_order(order_args)
                return self._clob_client.post_order(signed_order, OrderType.FOK)

            result = await asyncio.to_thread(_place)
            log.info("fok_order_placed", token_id=token_id, side=side, price=price, size=size)
            return result if result else {}
        except Exception as e:
            raise PolymarketAPIError(f"FOK order failed: {e}") from e

    async def place_limit_order(
        self, token_id: str, side: str, price: float, size: float
    ) -> dict:
        """Place a GTC limit order on the CLOB."""
        if not self._clob_client:
            raise PolymarketAPIError("CLOB client not initialized")

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            order_side = BUY if side.upper() == "BUY" else SELL
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=order_side,
            )

            def _place():
                signed_order = self._clob_client.create_order(order_args)
                return self._clob_client.post_order(signed_order, OrderType.GTC)

            result = await asyncio.to_thread(_place)
            log.info("gtc_order_placed", token_id=token_id, side=side, price=price, size=size)
            return result if result else {}
        except Exception as e:
            raise PolymarketAPIError(f"Order placement failed: {e}") from e

    async def get_order(self, order_id: str) -> dict:
        """Get current status and fill info for an order."""
        if not self._clob_client:
            return {}
        try:
            result = await asyncio.to_thread(self._clob_client.get_order, order_id)
            return result if result else {}
        except Exception as e:
            log.error("get_order_error", order_id=order_id, error=str(e))
            return {}

    async def cancel_order(self, order_id: str) -> bool:
        if not self._clob_client:
            return False
        try:
            await asyncio.to_thread(self._clob_client.cancel, order_id)
            return True
        except Exception as e:
            log.error("cancel_error", order_id=order_id, error=str(e))
            return False

    async def cancel_all_orders(self) -> bool:
        """Emergency: cancel all open orders for this account."""
        if not self._clob_client:
            return False
        try:
            await asyncio.to_thread(self._clob_client.cancel_all)
            log.info("all_orders_cancelled")
            return True
        except Exception as e:
            log.error("cancel_all_error", error=str(e))
            return False

    async def get_open_orders(self) -> list[dict]:
        if not self._clob_client:
            return []
        try:
            result = await asyncio.to_thread(self._clob_client.get_orders)
            return result if isinstance(result, list) else []
        except Exception as e:
            log.error("get_open_orders_error", error=str(e))
            return []

    def _wallet_address(self) -> str:
        if self._funder_address:
            return self._funder_address
        if self._clob_client and hasattr(self._clob_client, "get_address"):
            return self._clob_client.get_address() or ""
        return ""

    async def _get_conditional_balance_clob(self, token_id: str) -> float | None:
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=self._signature_type,
            )
            result = await asyncio.to_thread(
                self._clob_client.get_balance_allowance, params
            )
            raw = result.get("balance", 0) if isinstance(result, dict) else 0
            return float(raw) / 1e6
        except Exception:
            return None

    async def _get_conditional_balance_rpc(self, token_id: str) -> float | None:
        wallet = self._wallet_address()
        if not wallet or not self._clob_client:
            return None

        conditional_address = self._clob_client.get_conditional_address()
        if not conditional_address:
            return None

        try:
            from eth_utils import keccak
        except Exception as e:
            log.error("conditional_balance_eth_utils_missing", error=str(e))
            return None

        try:
            selector = keccak(text="balanceOf(address,uint256)")[:4].hex()
            wallet_arg = wallet.lower().removeprefix("0x").rjust(64, "0")
            token_int = int(str(token_id), 0)
            token_arg = hex(token_int)[2:].rjust(64, "0")
            result = await self._polygon_rpc(
                "eth_call",
                [
                    {
                        "to": conditional_address,
                        "data": f"0x{selector}{wallet_arg}{token_arg}",
                    },
                    "latest",
                ],
            )
            return int(result, 16) / 1e6
        except Exception as e:
            log.error(
                "conditional_balance_rpc_error",
                token_id=token_id,
                error=str(e),
            )
            return None

    async def get_positions(self, token_ids: list[str] | None = None) -> list[dict]:
        """Return user's outcome-token balances for the provided token IDs."""
        if not self._clob_client:
            return []
        token_ids = list(dict.fromkeys(token_ids or []))
        if not token_ids:
            return []

        positions: list[dict] = []
        for token_id in token_ids:
            balance = await self.get_conditional_balance(token_id)
            if balance and balance > 0:
                positions.append({
                    "token_id": token_id,
                    "shares": balance,
                })
        return positions

    async def close(self) -> None:
        await self._http.aclose()
