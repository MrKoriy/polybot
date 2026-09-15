import asyncio
import time
from dataclasses import dataclass
from datetime import datetime

import structlog

from core.exceptions import PolymarketAPIError
from data.polymarket_rest import PolymarketClient
from storage.repository import TradeRepository
from trading.risk_manager import TradeProposal

log = structlog.get_logger()

# Bankroll threshold below which FOK orders are used instead of GTC limit orders.
# FOK fills immediately or cancels — no heartbeat needed, ideal for small accounts.
# Lowered from $50 to $20 (Apr 27): with $48 bankroll, FOK was rejecting all
# weather trades because tail-bucket orderbooks have huge spreads (best ask $0.99
# vs our limit $0.04-0.54). GTC posts a passive bid and waits for fills, which
# is the correct behaviour for thin markets.
_FOK_BANKROLL_THRESHOLD = 20.0

# Maximum slippage fraction allowed on FOK orders (hard cap).
_FOK_MAX_SLIPPAGE = 0.02

# Fraction of edge we're willing to give as slippage (e.g. 25% of edge).
_FOK_SLIPPAGE_EDGE_RATIO = 0.25

# Minimum slippage floor so orders have a chance to fill.
_FOK_MIN_SLIPPAGE = 0.003

# GTC fill poll interval in seconds.
_GTC_POLL_INTERVAL = 5

# Maximum time (seconds) to wait for a GTC order to fill before giving up.
_GTC_TIMEOUT = 300

# Do not immediately re-place the same timed-out/cancelled GTC order next cycle.
_GTC_MARKET_COOLDOWN = 15 * 60


def _snap_price(price: float, tick_size: float) -> float:
    """Round price to the nearest valid tick, clamped to (0, 1) exclusive.

    Clamping is critical: an unclamped round-to-nearest can produce 0.0
    (invalid) from a 0.001 floor on a 0.01-tick book, or 1.0 from a 0.999
    cap — both make the CLOB reject the order and previously triggered
    _emergency_cancel_all of every unrelated open order.
    """
    if tick_size <= 0:
        return max(0.001, min(0.999, price))
    ticks = round(price / tick_size)
    snapped = round(ticks * tick_size, 6)
    return max(tick_size, min(1.0 - tick_size, snapped))


_SYSTEMIC_API_ERROR_MARKERS = (
    "401", "403", "unauthorized", "not authorized", "signature",
    "auth", "invalid api key", "funder mismatch",
)


def _is_systemic_api_error(error: str) -> bool:
    """True only for auth/signing/connectivity failures where we can no longer
    trust our ability to manage ANY open order. Order-level rejections (bad
    price, insufficient balance, invalid size) are NOT systemic and must not
    trigger cancel-all of unrelated resting orders.
    """
    err = (error or "").lower()
    return any(marker in err for marker in _SYSTEMIC_API_ERROR_MARKERS)


@dataclass
class ExecutionResult:
    success: bool
    trade_id: int = 0
    order_id: str = ""
    fill_price: float = 0.0
    fill_size: float = 0.0
    error: str = ""
    paper_mode: bool = True
    pending: bool = False


class OrderExecutor:
    def __init__(
        self,
        polymarket: PolymarketClient,
        trade_repo: TradeRepository,
        paper_mode: bool = True,
        on_gtc_fill=None,
        on_gtc_cancel=None,
        telegram=None,
        risk_manager=None,
        realistic_paper_execution: bool = False,
    ) -> None:
        self._polymarket = polymarket
        self._trade_repo = trade_repo
        self._paper_mode = paper_mode
        self._realistic_paper_execution = realistic_paper_execution
        # Callbacks for GTC order lifecycle events
        self._on_gtc_fill = on_gtc_fill  # async fn(trade_id, fill_price, fill_size, proposal_data)
        self._on_gtc_cancel = on_gtc_cancel  # async fn(trade_id, proposal_data)
        # Tracks background tasks polling GTC order fills: order_id -> asyncio.Task
        self._gtc_tasks: dict[str, asyncio.Task] = {}
        # Stash proposal data for GTC callbacks: order_id -> dict
        self._gtc_proposals: dict[str, dict] = {}
        # Market cooldown after GTC timeout/cancel: market_id -> monotonic expiry.
        self._gtc_market_cooldowns: dict[str, float] = {}
        # H4: alerting + execution backoff
        self._telegram = telegram               # TelegramNotifier | None
        self._risk_manager = risk_manager       # RiskManager | None
        self._consecutive_failures = 0          # reset on successful execute

    @property
    def pending_orders(self) -> list[dict]:
        return list(self._gtc_proposals.values())

    @property
    def realistic_paper_execution(self) -> bool:
        return self._realistic_paper_execution

    @property
    def pending_market_ids(self) -> set[str]:
        self._prune_gtc_cooldowns()
        return {
            str(data.get("market_id", ""))
            for data in self._gtc_proposals.values()
            if data.get("market_id")
        } | set(self._gtc_market_cooldowns.keys())

    def _prune_gtc_cooldowns(self) -> None:
        now = time.monotonic()
        expired = [mid for mid, until in self._gtc_market_cooldowns.items() if until <= now]
        for market_id in expired:
            self._gtc_market_cooldowns.pop(market_id, None)

    def _cooldown_gtc_market(self, proposal_data: dict | None, reason: str) -> None:
        if not proposal_data:
            return
        market_id = str(proposal_data.get("market_id") or "")
        self._cooldown_market_id(market_id, reason)

    def _cooldown_market_id(self, market_id: str, reason: str) -> None:
        if not market_id:
            return
        self._gtc_market_cooldowns[market_id] = time.monotonic() + _GTC_MARKET_COOLDOWN
        log.info(
            "gtc_market_cooldown_set",
            market_id=market_id,
            reason=reason,
            cooldown_s=_GTC_MARKET_COOLDOWN,
        )

    async def execute(
        self,
        proposal: TradeProposal,
        require_fill: bool = False,
    ) -> ExecutionResult:
        if self._paper_mode:
            return await self._paper_execute(proposal)
        return await self._live_execute(proposal, require_fill=require_fill)

    async def _paper_execute(self, proposal: TradeProposal) -> ExecutionResult:
        """Simulate trade execution at current market price."""
        if self._realistic_paper_execution:
            fill = await self._simulate_realistic_paper_fill(proposal)
            if not fill["success"]:
                return ExecutionResult(
                    success=False,
                    error=fill["error"],
                    paper_mode=True,
                )
            fill_price = float(fill["fill_price"])
            fill_size = float(fill["fill_size"])
            reasoning = (
                f"{proposal.reasoning} | realistic_paper "
                f"side={fill['side']} limit={proposal.price:.4f} "
                f"vwap={fill['pre_fee_price']:.4f} "
                f"fee={fill['fee_rate']*100:.2f}% "
                f"shares={fill['shares']:.6f}"
            )
            trade = await self._trade_repo.create_trade(
                market_id=proposal.market_id,
                token_id=proposal.token_id,
                question=proposal.question,
                side="BUY" if fill["side"] == "BUY" else "SELL",
                direction=proposal.direction,
                price=fill_price,
                size=fill_size,
                edge=proposal.edge,
                kelly_fraction=proposal.kelly_fraction,
                estimated_prob=proposal.estimated_prob,
                market_price_at_entry=fill_price,
                confidence=proposal.confidence,
                status="filled",
                paper_mode=True,
                order_id=f"paper_realistic_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}",
                reasoning=reasoning,
            )
            log.info(
                "paper_realistic_trade_filled",
                trade_id=trade.id,
                market=proposal.question[:50],
                direction=proposal.direction,
                limit_price=proposal.price,
                fill_price=fill_price,
                fill_size=fill_size,
            )
            return ExecutionResult(
                success=True,
                trade_id=trade.id,
                order_id=trade.order_id,
                fill_price=fill_price,
                fill_size=fill_size,
                paper_mode=True,
            )

        trade = await self._trade_repo.create_trade(
            market_id=proposal.market_id,
            token_id=proposal.token_id,
            question=proposal.question,
            side="BUY",
            direction=proposal.direction,
            price=proposal.price,
            size=proposal.size,
            edge=proposal.edge,
            kelly_fraction=proposal.kelly_fraction,
            estimated_prob=proposal.estimated_prob,
            market_price_at_entry=proposal.price,
            confidence=proposal.confidence,
            status="filled",
            paper_mode=True,
            order_id=f"paper_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}",
            reasoning=proposal.reasoning,
        )

        log.info(
            "paper_trade_executed",
            trade_id=trade.id,
            market=proposal.question[:50],
            direction=proposal.direction,
            price=proposal.price,
            size=proposal.size,
        )

        return ExecutionResult(
            success=True,
            trade_id=trade.id,
            order_id=trade.order_id,
            fill_price=proposal.price,
            fill_size=proposal.size,
            paper_mode=True,
        )

    # Conservative default taker fee when the CLOB /fee-rate endpoint is
    # unreachable. py_clob_client signs live orders with the market's dynamic
    # fee automatically, so paper must assume fees exist even when the lookup
    # fails — understating live costs is what fabricated the paper edge.
    _PAPER_DEFAULT_TAKER_FEE = 0.018

    async def _simulate_realistic_paper_fill(self, proposal: TradeProposal) -> dict:
        """Conservative CLOB fill simulator for paper mode.

        It only fills when current public book depth would execute the whole
        order immediately at the proposed limit. Passive maker fills are not
        assumed, so this understates rather than overstates live performance.

        Includes the dynamic taker fee (same fee py_clob_client signs into
        live orders) so paper costs match live costs.
        """
        side = "SELL" if (proposal.direction or "").upper().startswith("SELL") else "BUY"
        book = await self._polymarket.get_orderbook(proposal.token_id)
        levels = book.get("asks" if side == "BUY" else "bids", []) or []
        if not levels:
            log.info(
                "paper_realistic_no_book",
                token_id=proposal.token_id,
                direction=proposal.direction,
            )
            return {"success": False, "error": "paper_realistic_no_orderbook"}

        desired_shares = proposal.size / proposal.price if proposal.price > 0 else 0.0
        if desired_shares <= 0:
            return {"success": False, "error": "paper_realistic_invalid_size"}

        sorted_levels = sorted(
            levels,
            key=lambda lvl: float(lvl.get("price", 0) or 0),
            reverse=(side == "SELL"),
        )

        remaining = desired_shares
        shares = 0.0
        notional = 0.0
        for lvl in sorted_levels:
            try:
                level_price = float(lvl.get("price", 0) or 0)
                level_shares = float(lvl.get("size", 0) or 0)
            except (TypeError, ValueError):
                continue
            if level_price <= 0 or level_shares <= 0:
                continue
            if side == "BUY" and level_price > proposal.price:
                break
            if side == "SELL" and level_price < proposal.price:
                break
            take = min(remaining, level_shares)
            shares += take
            notional += take * level_price
            remaining -= take
            if remaining <= max(desired_shares * 0.001, 1e-6):
                break

        if shares < desired_shares * 0.995:
            best = sorted_levels[0].get("price", "n/a") if sorted_levels else "n/a"
            log.info(
                "paper_realistic_unfilled",
                token_id=proposal.token_id,
                direction=proposal.direction,
                limit_price=proposal.price,
                best_price=best,
                desired_shares=round(desired_shares, 6),
                fillable_shares=round(shares, 6),
            )
            return {
                "success": False,
                "error": (
                    "paper_realistic_unfilled "
                    f"(limit={proposal.price:.4f}, best={best}, "
                    f"fillable={shares:.4f}/{desired_shares:.4f})"
                ),
            }

        fill_price = notional / shares if shares > 0 else 0.0

        # Dynamic taker fee — the same fee live orders are signed with.
        fee_rate = await self._polymarket.get_fee_rate(proposal.token_id)
        if fee_rate is None:
            fee_rate = self._PAPER_DEFAULT_TAKER_FEE
        total_cost = notional * (1.0 + fee_rate)
        effective_price = fill_price * (1.0 + fee_rate)

        return {
            "success": True,
            "side": side,
            "fill_price": effective_price,
            "fill_size": total_cost,
            "shares": shares,
            "fee_rate": fee_rate,
            "fee_cost": notional * fee_rate,
            "pre_fee_price": fill_price,
        }

    async def _live_execute(
        self,
        proposal: TradeProposal,
        require_fill: bool = False,
    ) -> ExecutionResult:
        """Place real order via Polymarket CLOB API.

        Uses FOK (Fill-or-Kill) for small bankrolls (<$50): fills immediately at the
        requested price or cancels — no heartbeat required.

        Uses GTC (Good-Till-Cancelled) for larger bankrolls, with a background poll
        task that tracks fill status and updates the DB.
        """
        # 0. RUNAWAY-SIZE SAFETY: reject anything > $500 in live. If a strategy
        # somehow computed $3K from a bad Kelly calc, we refuse instead of firing.
        if proposal.size > 500.0:
            log.error("live_runaway_size_rejected",
                      size=proposal.size,
                      market=proposal.market_id,
                      direction=proposal.direction)
            return ExecutionResult(
                success=False,
                error=f"runaway size ${proposal.size:.2f} > $500 cap",
                paper_mode=False,
            )
        try:
            # 1. Check USDC balance before placing any order.
            balance = await self._polymarket.get_balance()
            if balance < proposal.size:
                return ExecutionResult(
                    success=False,
                    error=f"Insufficient USDC balance: ${balance:.4f} < required ${proposal.size:.4f}",
                    paper_mode=False,
                )

            # 2. Get market tick size for proper price rounding.
            tick_size = await self._polymarket.get_tick_size(proposal.token_id)

            # 3. Snap proposed price to the nearest valid tick.
            target_price = _snap_price(proposal.price, tick_size)

            # 4. Choose execution strategy based on current balance.
            if require_fill or balance < _FOK_BANKROLL_THRESHOLD:
                result = await self._execute_fok(proposal, target_price, tick_size)
                if not result.success and (
                    result.error.startswith("fok_below_best_ask")
                    or result.error.startswith("fok_above_best_bid")
                    or result.error.startswith("empty_orderbook")
                ):
                    self._cooldown_market_id(proposal.market_id, "fok_unfillable")
                return result
            else:
                return await self._execute_gtc(proposal, target_price)

        except PolymarketAPIError as e:
            log.error("live_execution_api_error", error=str(e))
            # Cancel-all ONLY for systemic failures (auth/signing/connectivity) —
            # we can no longer trust our ability to manage open orders. Order-
            # level rejections (bad price/size/insufficient balance) must NOT
            # nuke unrelated resting GTCs as collateral damage.
            if _is_systemic_api_error(str(e)):
                await self._emergency_cancel_all()
            return ExecutionResult(success=False, error=str(e), paper_mode=False)

        except Exception as e:
            log.error("live_execution_error", error=str(e), exc_info=True)
            return ExecutionResult(success=False, error=str(e), paper_mode=False)

    async def _execute_fok(
        self,
        proposal: TradeProposal,
        target_price: float,
        tick_size: float,
        persist_trade: bool = True,
    ) -> ExecutionResult:
        """Place FOK order with dynamic slippage proportional to edge size.

        FOK fills entirely at the bid price or cancels. Slippage is capped at 25%
        of the detected edge so we never give away more than a quarter of our profit.

        H5: sanity-check real orderbook before submitting. If best-ask is ABOVE our
        proposed fok_price on a BUY (or best-bid is BELOW on a SELL), FOK will
        cancel 100% — we reject early instead of wasting an API call. Also cap
        fok_price at best_ask*1.01 to avoid overpaying into distorted thin books.
        """
        # Derive CLOB side from proposal.direction (e.g. BUY_YES, SELL_YES, etc.)
        clob_side = "SELL" if (proposal.direction or "").upper().startswith("SELL") else "BUY"

        # Dynamic slippage: give up at most 25% of edge, bounded by min/max
        dynamic_slippage = max(
            _FOK_MIN_SLIPPAGE,
            min(_FOK_MAX_SLIPPAGE, abs(proposal.edge) * _FOK_SLIPPAGE_EDGE_RATIO),
        )
        if clob_side == "BUY":
            fok_price = _snap_price(min(target_price * (1 + dynamic_slippage), 0.999), tick_size)
        else:
            fok_price = _snap_price(max(target_price * (1 - dynamic_slippage), 0.001), tick_size)

        # H5: orderbook peek before firing
        try:
            book = await self._polymarket.get_orderbook(proposal.token_id)
        except Exception:
            book = {"bids": [], "asks": []}
        if clob_side == "BUY":
            asks = book.get("asks", []) or []
            if not asks:
                log.warning("fok_no_asks", token_id=proposal.token_id)
                return ExecutionResult(success=False,
                                       error="empty_orderbook (no asks)",
                                       paper_mode=False)
            best_ask = float(asks[0].get("price", 0) or 0)
            if best_ask > 0:
                # Cap fok_price so we don't overpay into distorted books
                fok_price = _snap_price(min(fok_price, best_ask * 1.01), tick_size)
                # If our capped price is below best-ask, FOK will cancel — abort.
                if fok_price < best_ask:
                    log.info("fok_below_ask_skipped",
                             fok_price=fok_price, best_ask=best_ask)
                    return ExecutionResult(
                        success=False,
                        error=f"fok_below_best_ask (${fok_price:.4f} < ${best_ask:.4f})",
                        paper_mode=False,
                    )
        else:  # SELL
            bids = book.get("bids", []) or []
            if not bids:
                log.warning("fok_no_bids", token_id=proposal.token_id)
                return ExecutionResult(success=False,
                                       error="empty_orderbook (no bids)",
                                       paper_mode=False)
            best_bid = float(bids[0].get("price", 0) or 0)
            if best_bid > 0:
                # For SELL, we want price ≤ best_bid to fill.
                fok_price = _snap_price(max(fok_price, best_bid * 0.99), tick_size)
                if fok_price > best_bid:
                    log.info("fok_above_bid_skipped",
                             fok_price=fok_price, best_bid=best_bid)
                    return ExecutionResult(
                        success=False,
                        error=f"fok_above_best_bid (${fok_price:.4f} > ${best_bid:.4f})",
                        paper_mode=False,
                    )

        # Convert dollar amount to shares.
        shares = proposal.size / target_price

        log.info(
            "fok_order_attempt",
            market=proposal.question[:50],
            side=clob_side,
            target_price=target_price,
            fok_price=fok_price,
            shares=shares,
            dollar_size=proposal.size,
        )

        result = await self._polymarket.place_fok_order(
            token_id=proposal.token_id,
            side=clob_side,
            price=fok_price,
            size=shares,
        )

        order_id = result.get("orderID", result.get("id", ""))
        order_status = (result.get("status", "") or "").upper()

        # FOK orders either MATCH (full fill) or CANCEL (no fill).
        if order_status in ("CANCELLED", "CANCELED") or not order_id:
            log.warning(
                "fok_order_not_filled",
                token_id=proposal.token_id,
                side=clob_side,
                fok_price=fok_price,
                status=order_status,
                response=result,
            )
            return ExecutionResult(
                success=False,
                error=f"FOK order not filled (status={order_status}): no matching liquidity at ${fok_price:.4f}",
                paper_mode=False,
            )

        # Extract ACTUAL fill details. The raw POST /order response usually
        # lacks price/size_matched — previously the DB recorded the
        # pre-slippage target_price, overstating live PnL while the wallet
        # paid fok_price (ask + slippage). Query the order for real numbers;
        # fall back to fok_price (the price we actually sent), never the
        # optimistic target.
        size_matched_str = result.get("size_matched", "") or ""
        shares_filled = float(size_matched_str) if size_matched_str else shares
        fill_price = float(result.get("price", 0) or 0)

        if not fill_price or not size_matched_str:
            try:
                order = await self._polymarket.get_order(order_id)
                if order:
                    sm = order.get("size_matched", "") or ""
                    if sm:
                        shares_filled = float(sm)
                    op = order.get("price", "") or ""
                    if op:
                        fill_price = float(op)
            except Exception:
                log.debug("fok_fill_lookup_failed", order_id=order_id)

        if fill_price <= 0:
            # Honest fallback: the price actually sent (includes slippage),
            # not the pre-slippage target.
            fill_price = fok_price
        fill_size_usd = shares_filled * fill_price

        trade_id = 0
        if persist_trade:
            trade = await self._trade_repo.create_trade(
                market_id=proposal.market_id,
                token_id=proposal.token_id,
                question=proposal.question,
                side=clob_side,
                direction=proposal.direction,
                price=fill_price,
                size=fill_size_usd,
                edge=proposal.edge,
                kelly_fraction=proposal.kelly_fraction,
                estimated_prob=proposal.estimated_prob,
                market_price_at_entry=proposal.price,
                confidence=proposal.confidence,
                status="filled",
                paper_mode=False,
                order_id=order_id,
                reasoning=proposal.reasoning,
            )
            trade_id = trade.id

        log.info(
            "fok_trade_executed",
            trade_id=trade_id,
            order_id=order_id,
            market=proposal.question[:50],
            fill_price=fill_price,
            fill_size_usd=fill_size_usd,
        )

        self._reset_failure_counter()
        return ExecutionResult(
            success=True,
            trade_id=trade_id,
            order_id=order_id,
            fill_price=fill_price,
            fill_size=fill_size_usd,
            paper_mode=False,
        )

    async def _execute_gtc(
        self, proposal: TradeProposal, target_price: float
    ) -> ExecutionResult:
        """Place GTC limit order and start a background fill-tracker task.

        GTC orders stay on the book until filled or cancelled. We spawn a poll task
        that checks every _GTC_POLL_INTERVAL seconds and updates the DB on fill.
        """
        shares = proposal.size / target_price

        # Derive CLOB side from proposal.direction (supports BUY_YES, SELL_YES, etc.)
        clob_side = "SELL" if (proposal.direction or "").upper().startswith("SELL") else "BUY"

        result = await self._polymarket.place_limit_order(
            token_id=proposal.token_id,
            side=clob_side,
            price=target_price,
            size=shares,
        )

        order_id = result.get("orderID", result.get("id", ""))
        if not order_id:
            return ExecutionResult(
                success=False,
                error=f"GTC order placement returned no order ID: {result}",
                paper_mode=False,
            )

        trade = await self._trade_repo.create_trade(
            market_id=proposal.market_id,
            token_id=proposal.token_id,
            question=proposal.question,
            side=clob_side,
            direction=proposal.direction,
            price=target_price,
            size=proposal.size,
            edge=proposal.edge,
            kelly_fraction=proposal.kelly_fraction,
            estimated_prob=proposal.estimated_prob,
            market_price_at_entry=proposal.price,
            confidence=proposal.confidence,
            status="open",
            paper_mode=False,
            order_id=order_id,
            reasoning=proposal.reasoning,
        )

        # Stash proposal data so the poll callback can open the position later
        self._gtc_proposals[order_id] = {
            "market_id": proposal.market_id,
            "token_id": proposal.token_id,
            "question": proposal.question,
            "direction": proposal.direction,
            "size": proposal.size,
            "estimated_prob": proposal.estimated_prob,
            "confidence": proposal.confidence,
            "edge": proposal.edge,
            "kelly_fraction": proposal.kelly_fraction,
            "reasoning": proposal.reasoning,
            "market_price": proposal.price,
            "metadata": dict(proposal.metadata or {}),
        }

        # Spawn background heartbeat/fill-poll task.
        task = asyncio.create_task(
            self._poll_gtc_fill(order_id, trade.id),
            name=f"gtc_poll_{order_id[:8]}",
        )
        self._gtc_tasks[order_id] = task

        log.info(
            "gtc_order_placed",
            trade_id=trade.id,
            order_id=order_id,
            market=proposal.question[:50],
            price=target_price,
            shares=shares,
        )

        # GTC: order is placed but not filled yet. Position will be opened only
        # after the async fill callback confirms the real execution.
        self._reset_failure_counter()
        return ExecutionResult(
            success=True,
            trade_id=trade.id,
            order_id=order_id,
            fill_price=0.0,
            fill_size=0.0,
            paper_mode=False,
            pending=True,
        )

    async def _poll_gtc_fill(self, order_id: str, trade_id: int) -> None:
        """Background task: poll CLOB every _GTC_POLL_INTERVAL seconds until fill or timeout.

        Handles statuses: MATCHED/FILLED/SETTLED (filled), CANCELLED (with
        partial-fill salvage), timeout (cancel + partial-fill salvage).
        Partial fills previously vanished: a partially-filled resting order
        that then cancelled was marked 'cancelled' even though real shares
        were bought on-chain — orphaning tokens and corrupting bankroll math.
        """
        elapsed = 0
        try:
            while elapsed < _GTC_TIMEOUT:
                await asyncio.sleep(_GTC_POLL_INTERVAL)
                elapsed += _GTC_POLL_INTERVAL

                order = await self._polymarket.get_order(order_id)
                status = (order.get("status", "") or "").upper()

                if status in ("MATCHED", "FILLED", "SETTLED"):
                    await self._handle_gtc_filled(order_id, trade_id, order)
                    break

                elif status in ("CANCELLED", "CANCELED"):
                    await self._handle_gtc_cancelled(order_id, trade_id, order, "cancelled")
                    break
                # LIVE / DELAYED / unknown → keep waiting.

            else:
                # Timeout — cancel the order, then salvage any partial fill.
                log.warning("gtc_order_timeout", order_id=order_id, trade_id=trade_id, elapsed=elapsed)
                try:
                    await self._polymarket.cancel_order(order_id)
                except Exception:
                    log.debug("gtc_timeout_cancel_failed", order_id=order_id)
                order = {}
                try:
                    order = await self._polymarket.get_order(order_id)
                except Exception:
                    pass
                await self._handle_gtc_cancelled(order_id, trade_id, order, "timeout")

        except Exception as e:
            log.error("gtc_poll_error", order_id=order_id, trade_id=trade_id, error=str(e))
        finally:
            self._gtc_tasks.pop(order_id, None)

    async def _handle_gtc_filled(self, order_id: str, trade_id: int, order: dict) -> None:
        size_matched_str = order.get("size_matched", "") or ""
        fill_price_str = order.get("price", "0") or "0"
        shares_filled = float(size_matched_str) if size_matched_str else 0.0
        fill_price = float(fill_price_str)
        fill_size_usd = shares_filled * fill_price if shares_filled > 0 else 0.0

        await self._trade_repo.update_trade_status(
            trade_id,
            status="filled",
            fill_price=fill_price if fill_price > 0 else 0.0,
            fill_size=fill_size_usd if fill_size_usd > 0 else 0.0,
        )
        log.info("gtc_order_filled", order_id=order_id, trade_id=trade_id, fill_price=fill_price)

        proposal_data = self._gtc_proposals.pop(order_id, None)
        if self._on_gtc_fill and proposal_data:
            try:
                await self._on_gtc_fill(trade_id, fill_price, fill_size_usd, proposal_data)
            except Exception:
                log.exception("gtc_fill_callback_error", order_id=order_id)

    async def _handle_gtc_cancelled(
        self, order_id: str, trade_id: int, order: dict, reason: str
    ) -> None:
        """Cancelled/timeout GTC — salvage real partial fills before writing off.

        A cancelled order with size_matched > 0 has ALREADY bought shares
        on-chain. Those must be recorded as a filled trade and opened as a
        position, or the shares are orphaned (invisible to portfolio,
        untracked forever).
        """
        size_matched_str = order.get("size_matched", "") or ""
        shares_matched = float(size_matched_str) if size_matched_str else 0.0

        if shares_matched > 0:
            fill_price_str = order.get("price", "0") or "0"
            try:
                fill_price = float(fill_price_str)
            except (TypeError, ValueError):
                fill_price = 0.0
            if fill_price <= 0:
                # fall back to the proposal's target price for the partial leg
                proposal_data = self._gtc_proposals.get(order_id) or {}
                fill_price = float(proposal_data.get("market_price") or 0.0)
            fill_size_usd = shares_matched * fill_price

            if fill_price > 0 and fill_size_usd > 0:
                log.warning(
                    "gtc_partial_fill_salvaged",
                    order_id=order_id,
                    trade_id=trade_id,
                    reason=reason,
                    shares=round(shares_matched, 6),
                    fill_price=fill_price,
                )
                await self._handle_gtc_filled(order_id, trade_id, {
                    "size_matched": size_matched_str,
                    "price": str(fill_price),
                })
                return

        await self._trade_repo.update_trade_status(trade_id, status="cancelled")
        log.info("gtc_order_cancelled", order_id=order_id, trade_id=trade_id, reason=reason)

        proposal_data = self._gtc_proposals.pop(order_id, None)
        self._cooldown_gtc_market(proposal_data, reason)
        if self._on_gtc_cancel and proposal_data:
            try:
                await self._on_gtc_cancel(trade_id, proposal_data)
            except Exception:
                log.exception("gtc_cancel_callback_error", order_id=order_id)

    async def close_live(self, position, reason: str = "resolution") -> ExecutionResult:
        """Close a live position. Called from trader close paths
        when a position needs to be liquidated (resolution, TP, SL, backstop).

        If the market is already officially resolved on-chain, CLOB books are
        often gone. In that case the correct close path is CTF redeem, and the
        realised payout comes from the transaction receipt.

        Returns ExecutionResult with fill_price that should be passed to
        Portfolio.close_position(market_id, fill_price) to realise the PnL.

        Rejects in paper mode — paper uses deterministic oracle price.
        """
        if self._paper_mode:
            return ExecutionResult(
                success=False,
                error="close_live called in paper_mode (use Portfolio.close_position with oracle price)",
                paper_mode=True,
            )

        # Officially resolved markets must be redeemed on-chain. Selling after
        # CLOB delists the token fails with "no orderbook/no midpoint".
        try:
            payouts = await self._polymarket.get_condition_payouts(position.market_id)
        except Exception as e:
            payouts = {"resolved": False}
            log.debug("condition_payout_check_failed",
                      market_id=position.market_id, error=str(e))

        if payouts.get("resolved"):
            try:
                redeem = await self._polymarket.redeem_resolved_position(
                    position.market_id,
                    position.token_id,
                )
                if not redeem.get("success"):
                    return ExecutionResult(
                        success=False,
                        error=redeem.get("error", "redeem_failed"),
                        paper_mode=False,
                    )

                payout = float(redeem.get("payout", 0.0) or 0.0)
                db_shares = position.size / position.entry_price if position.entry_price > 0 else 0.0
                fill_price = payout / db_shares if db_shares > 0 else 0.0
                log.info(
                    "close_live_redeemed",
                    market_id=position.market_id,
                    token_id=position.token_id,
                    tx_hash=redeem.get("tx_hash", ""),
                    payout=round(payout, 6),
                    fill_price=round(fill_price, 6),
                    shares=round(float(redeem.get("shares", 0.0) or 0.0), 6),
                    reason=reason,
                )
                return ExecutionResult(
                    success=True,
                    order_id=redeem.get("tx_hash", ""),
                    fill_price=fill_price,
                    fill_size=payout,
                    paper_mode=False,
                )
            except Exception as e:
                log.exception("close_live_redeem_error", market_id=position.market_id)
                return ExecutionResult(
                    success=False,
                    error=f"redeem exception: {e}",
                    paper_mode=False,
                )

        # Query current mid-price for the token we hold
        mid = await self._polymarket.get_midpoint(position.token_id)
        if not mid or mid <= 0:
            return ExecutionResult(
                success=False,
                error="awaiting_onchain_resolution:no_mid_price",
                paper_mode=False,
            )

        shares_held = position.size / position.entry_price if position.entry_price > 0 else 0
        if shares_held <= 0:
            return ExecutionResult(
                success=False,
                error="invalid_shares_held",
                paper_mode=False,
            )

        # SELL target: 1¢ below mid for better fill probability. Snap to tick.
        tick = await self._polymarket.get_tick_size(position.token_id)
        raw_target = max(0.02, min(0.99, mid - 0.01))
        target_price = _snap_price(raw_target, tick)

        # Build a SELL proposal. We express size in USDC-equivalent (shares × target),
        # consistent with buy-side convention. Note: direction becomes SELL_* so
        # executor.place_*_order's `clob_side` derivation picks SELL.
        direction = (position.direction or "").upper().replace("BUY_", "SELL_", 1)
        if not direction.startswith("SELL"):
            direction = "SELL_" + direction  # defensive fallback

        sell_proposal = TradeProposal(
            market_id=position.market_id,
            token_id=position.token_id,
            question=position.question,
            direction=direction,
            price=target_price,
            size=shares_held * target_price,
            edge=0.0,
            estimated_prob=0.0,
            confidence=0.0,
            kelly_fraction=0.0,
            reasoning=f"close_live: {reason} (mid={mid:.3f})",
        )

        log.info("close_live_attempt",
                 market_id=position.market_id,
                 direction=direction,
                 shares=round(shares_held, 4),
                 mid=round(mid, 4),
                 target_price=target_price,
                 reason=reason)

        try:
            # Live exits must be deterministic: only report success after an
            # actual fill, never when a resting sell order is merely placed.
            return await self._execute_fok(
                sell_proposal,
                target_price,
                tick,
                persist_trade=False,
            )
        except Exception as e:
            log.exception("close_live_error", market_id=position.market_id)
            return ExecutionResult(
                success=False,
                error=f"close_live exception: {e}",
                paper_mode=False,
            )

    async def _emergency_cancel_all(self) -> None:
        """Cancel all open orders on critical errors to avoid runaway positions.

        Increments consecutive-failure counter. On 3+ consecutive failures,
        sends Telegram alert and asks risk_manager to pause the bot for 1h.
        """
        self._consecutive_failures += 1
        try:
            cancelled = await self._polymarket.cancel_all_orders()
            if cancelled:
                log.info("emergency_cancel_all_completed",
                         consecutive=self._consecutive_failures)
        except Exception as e:
            log.error("emergency_cancel_failed", error=str(e))

        # H4: alert + backoff on repeated failures.
        if self._telegram:
            try:
                await self._telegram.send_risk_alert(
                    "emergency_cancel",
                    f"Execution failure #{self._consecutive_failures}. "
                    f"{'⚠ AUTO-PAUSE triggered' if self._consecutive_failures >= 3 else 'Will pause on 3+'}",
                )
            except Exception:
                log.exception("alert_send_failed")

        if self._consecutive_failures >= 3 and self._risk_manager:
            try:
                self._risk_manager.pause(hours=1.0)
                log.error("auto_paused_after_failures",
                          consecutive=self._consecutive_failures,
                          pause_hours=1.0)
            except Exception:
                log.exception("auto_pause_failed")

    def _reset_failure_counter(self) -> None:
        """Called by caller after a successful execution to clear backoff."""
        if self._consecutive_failures > 0:
            log.info("execution_failure_streak_cleared",
                     was=self._consecutive_failures)
            self._consecutive_failures = 0

    async def cancel_pending_gtc_tasks(self) -> None:
        """Graceful shutdown: cancel all pending GTC poll tasks."""
        for order_id, task in list(self._gtc_tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._gtc_tasks.clear()
        log.info("gtc_tasks_cancelled")
