"""Spread Capture & Fee-Aware Crypto Trading.

Monitors both sides (Up/Down) of crypto 5m/15m markets.
When Up + Down + fees < $1.00 → buys both for guaranteed profit.
Also provides fee calculation for all crypto trades.

Uses Binance WebSocket as leading price indicator and Polymarket
prices for execution.
"""
import asyncio
import json
import time as _time
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from data.binance_ws import BinanceWSFeed
from storage.repository import TradeRepository
from trading.portfolio import Portfolio

log = structlog.get_logger()

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Crypto assets and their Polymarket slug prefixes
CRYPTO_SLUGS = {
    "BTC": {"5m": "btc-updown-5m", "15m": "btc-updown-15m"},
    "ETH": {"5m": "eth-updown-5m", "15m": "eth-updown-15m"},
    "SOL": {"5m": "sol-updown-5m", "15m": "sol-updown-15m"},
}

INTERVALS = {"5m": 300, "15m": 900}

# --- Spread capture params ---
# Minimum profit margin after fees to execute spread capture
MIN_SPREAD_PROFIT = 0.015  # 1.5% minimum guaranteed profit
# Maximum position for spread capture (fraction of bankroll)
MAX_SPREAD_POSITION = 0.15
# Absolute $ cap per leg (safety net for live mode)
MAX_SPREAD_BET_USDC = 200.0
# Check interval
CHECK_INTERVAL = 5  # 5 seconds — fast for spread capture

# --- Fee params ---
# Taker fee rate for crypto markets (dynamic, but typically ~1.8%)
DEFAULT_TAKER_FEE_RATE = 0.018
# Maker fee rate (0% + rebates)
MAKER_FEE_RATE = 0.0

# --- Last-second strategy ---
LAST_SECOND_WINDOW = 15  # seconds before resolution
LAST_SECOND_MIN_PRICE = 0.88  # minimum price for "winning" side


class MarketPair:
    """Both sides of a crypto Up/Down market."""

    def __init__(self, event_data: dict, market_data: dict) -> None:
        self.slug = event_data.get("slug", "")
        self.title = event_data.get("title", "")
        self.condition_id = market_data.get("conditionId", "")
        self.end_date_str = market_data.get("endDate", "")

        raw_prices = market_data.get("outcomePrices", '["0.5","0.5"]')
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        self.up_price = float(prices[0]) if prices else 0.5
        self.down_price = float(prices[1]) if len(prices) > 1 else 0.5

        raw_tokens = market_data.get("clobTokenIds", "[]")
        tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
        self.up_token = str(tokens[0]) if tokens else ""
        self.down_token = str(tokens[1]) if len(tokens) > 1 else ""

        try:
            self.end_date = datetime.fromisoformat(self.end_date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            self.end_date = None

    @property
    def seconds_left(self) -> float:
        if not self.end_date:
            return 0
        return (self.end_date - datetime.now(timezone.utc)).total_seconds()

    @property
    def combined_price(self) -> float:
        return self.up_price + self.down_price

    @property
    def spread_profit(self) -> float:
        """Profit per pair if buying both sides (before fees)."""
        return 1.0 - self.combined_price

    def spread_profit_after_fees(self, fee_rate: float) -> float:
        """Net profit per pair after taker fees on both sides."""
        fee_up = self.up_price * fee_rate
        fee_down = self.down_price * fee_rate
        return 1.0 - self.combined_price - fee_up - fee_down


class SpreadCaptureTrader:
    """Spread capture + fee-aware crypto trading."""

    def __init__(
        self,
        portfolio: Portfolio,
        binance_feed: BinanceWSFeed,
        paper_mode: bool = True,
        trade_repo: TradeRepository | None = None,
        executor=None,  # OrderExecutor — required for live mode
        breakers=None,  # CircuitBreakerManager — optional halt enforcement
    ) -> None:
        self._portfolio = portfolio
        self._binance = binance_feed
        self._paper_mode = paper_mode
        self._trade_repo = trade_repo
        self._executor = executor
        self._breakers = breakers
        self._http = httpx.AsyncClient(
            timeout=8.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PolyBot/1.0)"},
        )
        self._running = False
        self._cycle_count = 0
        self._spreads_captured = 0
        self._last_second_trades = 0

        # Track active spread positions to avoid doubling
        self._spread_positions: set[str] = set()  # condition_ids

        # Fee cache: token_id -> (fee_rate, timestamp)
        self._fee_cache: dict[str, tuple[float, float]] = {}

    async def start(self) -> None:
        self._running = True
        log.info("spread_capture_started")
        asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        await self._http.aclose()
        log.info("spread_capture_stopped", spreads=self._spreads_captured, last_second=self._last_second_trades)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._run_cycle()
            except Exception:
                log.exception("spread_capture_error")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _run_cycle(self) -> None:
        self._cycle_count += 1

        # Close expired spread/last-second positions
        await self._close_expired()

        if self._breakers is not None:
            halted, reason = self._breakers.is_halted("spread")
            if halted:
                log.info("spread_cycle_halted", reason=reason)
                return

        for crypto, slugs in CRYPTO_SLUGS.items():
            for tf, prefix in slugs.items():
                interval = INTERVALS[tf]
                market = await self._find_market(prefix, interval)
                if not market:
                    continue

                # Strategy 1: Spread capture (true arb — Up+Down+fees<$1)
                if market.seconds_left > 30:
                    await self._check_spread_capture(crypto, tf, market)

                # Strategy 2: Last-second — DISABLED. Historical "other" bucket
                # was 380 trades, +$11 PnL (87% WR but hidden negative EV via
                # hard tail-losses). Not worth the gas.
                # if LAST_SECOND_WINDOW >= market.seconds_left > 3:
                #     await self._check_last_second(crypto, tf, market)

    async def _close_expired(self) -> None:
        """Close spread/last-second positions that have resolved."""
        for pos in list(self._portfolio.open_positions):
            q = pos.question or ""
            if not (q.startswith("SPREAD ") or q.startswith("LASTSEC ")):
                continue
            if not pos.opened_at:
                continue
            opened = pos.opened_at.replace(tzinfo=timezone.utc) if pos.opened_at.tzinfo is None else pos.opened_at
            age_min = (datetime.now(timezone.utc) - opened).total_seconds() / 60
            # Spread/last-second positions should resolve within 5-15 min + buffer
            is_5m = "5m" in q
            max_age = 7.0 if is_5m else 17.0
            if age_min < max_age:
                continue

            # Spread: we bought BOTH Up and Down legs. One resolves 1.0, other 0.0.
            # The profit comes from combined_price < 1.0 at entry.
            # We need to determine which side won. Use Binance as source of truth.
            if "SPREAD" in q:
                crypto = None
                for c in CRYPTO_SLUGS:
                    if c in q:
                        crypto = c
                        break
                is_up_leg = "BUY_UP" in (pos.direction or "")
                # Try to get Binance direction
                binance_price = self._binance.get_price(crypto) if crypto else None
                # We can't perfectly know the winner from Binance alone without the
                # entry price, so use a heuristic: the Up side wins if binance shows
                # positive momentum. For spread profit this doesn't matter much since
                # we hold both sides — one gets 1.0, other gets 0.0, net = 1.0 - cost.
                # The critical fix: losing side MUST be 0.0, not 0.5.
                up_won = True  # default assumption
                if binance_price and crypto:
                    # Check recent price direction from Binance
                    up_won = True  # simplification; actual determination from market resolution
                exit_price = 1.0 if (is_up_leg == up_won) else 0.0
            else:
                # Last-second: likely won if price was >88%
                exit_price = 1.0 if pos.entry_price >= 0.85 else 0.0

            # LIVE: sell each leg on CLOB. PAPER: deterministic $1/$0.
            if self._executor and not self._paper_mode:
                result = await self._executor.close_live(pos, reason="spread_expired")
                if not result.success:
                    log.warning("spread_close_live_failed",
                                market=pos.market_id, error=result.error)
                    continue
                realised_exit = result.fill_price
            else:
                realised_exit = exit_price

            pnl = await self._portfolio.close_position(pos.market_id, realised_exit)
            log.info("spread_position_closed",
                     question=q[:50],
                     expected_exit=exit_price,
                     realised_exit=round(realised_exit, 4),
                     pnl=round(pnl, 4),
                     age=round(age_min, 1))

    async def _check_spread_capture(self, crypto: str, tf: str, market: MarketPair) -> None:
        """Check if both sides can be bought for < $1.00 after fees."""
        if market.condition_id in self._spread_positions:
            return

        # Get fee rate
        fee_rate = await self._get_fee_rate(market.up_token)

        net_profit = market.spread_profit_after_fees(fee_rate)

        if net_profit < MIN_SPREAD_PROFIT:
            return

        # Profitable spread found!
        bankroll = self._portfolio.bankroll
        if bankroll <= 0:
            return

        # Size: profit-proportional, capped by % of bankroll AND absolute $
        bet_per_side = min(
            bankroll * MAX_SPREAD_POSITION,
            bankroll * 0.5,
            MAX_SPREAD_BET_USDC,
        )
        shares = bet_per_side / max(market.up_price, 0.01)

        log.info(
            "spread_capture_found",
            crypto=crypto,
            tf=tf,
            up_price=market.up_price,
            down_price=market.down_price,
            combined=round(market.combined_price, 3),
            fee_rate=round(fee_rate, 4),
            net_profit_pct=round(net_profit * 100, 2),
            bet_per_side=round(bet_per_side, 2),
            seconds_left=round(market.seconds_left),
        )

        up_question = f"SPREAD {crypto} {tf} Up | {market.title[:30]}"
        down_question = f"SPREAD {crypto} {tf} Down | {market.title[:30]}"
        down_size = round(bet_per_side * market.down_price / max(market.up_price, 0.01), 2)
        up_size = round(bet_per_side, 2)

        # --- LIVE: both legs through OrderExecutor (atomic arb intent).
        # If either leg fails, abort — we'd be left with directional exposure.
        use_executor = self._executor and (
            not self._paper_mode
            or getattr(self._executor, "realistic_paper_execution", False)
        )
        if use_executor:
            from trading.risk_manager import TradeProposal

            up_proposal = TradeProposal(
                market_id=f"{market.condition_id}_UP",
                token_id=market.up_token,
                question=up_question,
                direction="BUY_UP",
                price=market.up_price,
                size=up_size,
                edge=net_profit,
                estimated_prob=0.5,
                confidence=0.5,
                kelly_fraction=0.0,
                reasoning="spread_capture",
            )
            up_result = await self._executor.execute(up_proposal, require_fill=True)
            if not up_result.success or up_result.pending:
                log.warning("spread_up_leg_failed", error=up_result.error)
                return

            down_proposal = TradeProposal(
                market_id=f"{market.condition_id}_DOWN",
                token_id=market.down_token,
                question=down_question,
                direction="BUY_DOWN",
                price=market.down_price,
                size=down_size,
                edge=net_profit,
                estimated_prob=0.5,
                confidence=0.5,
                kelly_fraction=0.0,
                reasoning="spread_capture",
            )
            down_result = await self._executor.execute(down_proposal, require_fill=True)
            if not down_result.success or down_result.pending:
                # Critical: UP leg filled but DOWN failed. Emergency-close UP.
                log.error("spread_down_leg_failed_after_up_filled",
                          error=down_result.error,
                          up_trade_id=up_result.trade_id)
                # Try to sell UP leg to flatten.
                from trading.portfolio import Position
                up_pos = Position(
                    trade_id=up_result.trade_id,
                    market_id=f"{market.condition_id}_UP",
                    token_id=market.up_token,
                    question=up_question,
                    direction="BUY_UP",
                    entry_price=up_result.fill_price or market.up_price,
                    size=up_result.fill_size or up_size,
                )
                close_res = await self._executor.close_live(up_pos, reason="spread_abort")
                log.warning("spread_abort_close_attempt", success=close_res.success)
                return

            up_trade_id = up_result.trade_id
            down_trade_id = down_result.trade_id
            up_fill_price = up_result.fill_price or market.up_price
            down_fill_price = down_result.fill_price or market.down_price
            up_fill_size = up_result.fill_size or up_size
            down_fill_size = down_result.fill_size or down_size

            self._portfolio.open_position(
                trade_id=up_trade_id,
                market_id=f"{market.condition_id}_UP",
                token_id=market.up_token,
                question=up_question,
                direction="BUY_UP",
                price=up_fill_price,
                size=up_fill_size,
            )
            self._portfolio.open_position(
                trade_id=down_trade_id,
                market_id=f"{market.condition_id}_DOWN",
                token_id=market.down_token,
                question=down_question,
                direction="BUY_DOWN",
                price=down_fill_price,
                size=down_fill_size,
            )
        else:
            # Paper: simulate both legs with trade records.
            up_trade_id = 0
            down_trade_id = 0
            if self._trade_repo:
                up_trade = await self._trade_repo.create_trade(
                    market_id=f"{market.condition_id}_UP", token_id=market.up_token,
                    question=up_question, side="BUY", direction="BUY_UP",
                    price=market.up_price, size=up_size, edge=net_profit,
                    kelly_fraction=0.0, estimated_prob=0.0, market_price_at_entry=market.up_price,
                    confidence=0.0, status="filled", paper_mode=self._paper_mode,
                    order_id=f"spread_up_{int(_time.time())}", reasoning="spread_capture",
                )
                up_trade_id = up_trade.id
                down_trade = await self._trade_repo.create_trade(
                    market_id=f"{market.condition_id}_DOWN", token_id=market.down_token,
                    question=down_question, side="BUY", direction="BUY_DOWN",
                    price=market.down_price, size=down_size, edge=net_profit,
                    kelly_fraction=0.0, estimated_prob=0.0, market_price_at_entry=market.down_price,
                    confidence=0.0, status="filled", paper_mode=self._paper_mode,
                    order_id=f"spread_down_{int(_time.time())}", reasoning="spread_capture",
                )
                down_trade_id = down_trade.id
            self._portfolio.open_position(
                trade_id=up_trade_id,
                market_id=f"{market.condition_id}_UP",
                token_id=market.up_token,
                question=up_question,
                direction="BUY_UP",
                price=market.up_price,
                size=up_size,
            )
            self._portfolio.open_position(
                trade_id=down_trade_id,
                market_id=f"{market.condition_id}_DOWN",
                token_id=market.down_token,
                question=down_question,
                direction="BUY_DOWN",
                price=market.down_price,
                size=down_size,
            )
        self._spread_positions.add(market.condition_id)
        self._spreads_captured += 1

        log.info(
            "spread_capture_executed",
            crypto=crypto,
            tf=tf,
            net_profit_pct=round(net_profit * 100, 2),
            combined_cost=round(market.combined_price, 3),
        )

    async def _check_last_second(self, crypto: str, tf: str, market: MarketPair) -> None:
        """Last-second strategy: bet on the winning side near resolution."""
        # Check Binance for current direction
        binance_price = self._binance.get_price(crypto)
        if not binance_price:
            return

        # Determine likely winner based on current price vs start
        # If up_price > 0.85 → Up is winning, if down_price > 0.85 → Down is winning
        if market.up_price >= LAST_SECOND_MIN_PRICE:
            direction = "Up"
            token = market.up_token
            price = market.up_price
        elif market.down_price >= LAST_SECOND_MIN_PRICE:
            direction = "Down"
            token = market.down_token
            price = market.down_price
        else:
            return  # Too uncertain

        # Check not already positioned
        key = f"LS_{market.condition_id}"
        if key in self._spread_positions:
            return

        bankroll = self._portfolio.bankroll
        if bankroll <= 0:
            return

        # Small bet — high confidence but limited upside
        bet_size = min(bankroll * 0.05, 2.0)
        if bet_size < 0.30:
            return

        profit_per_share = 1.0 - price  # e.g., buy at 0.92, win $1.00, profit $0.08/share
        if profit_per_share < 0.03:  # Less than 3% — not worth it
            return

        fee_rate = await self._get_fee_rate(token)
        if profit_per_share <= fee_rate * price:
            return  # Fee eats the profit

        log.info(
            "last_second_trade",
            crypto=crypto,
            tf=tf,
            direction=direction,
            price=price,
            profit_per_share=round(profit_per_share, 3),
            seconds_left=round(market.seconds_left),
            bet_size=bet_size,
        )

        ls_question = f"LASTSEC {crypto} {tf} {direction} | {market.title[:25]}"
        ls_dir = f"BUY_{direction.upper()}"
        ls_trade_id = 0
        if self._trade_repo:
            ls_trade = await self._trade_repo.create_trade(
                market_id=key, token_id=token, question=ls_question,
                side="BUY", direction=ls_dir, price=price, size=round(bet_size, 2),
                edge=profit_per_share, kelly_fraction=0.0, estimated_prob=0.0,
                market_price_at_entry=price, confidence=0.0, status="filled",
                paper_mode=self._paper_mode,
                order_id=f"lastsec_{int(_time.time())}", reasoning="last_second",
            )
            ls_trade_id = ls_trade.id

        self._portfolio.open_position(
            trade_id=ls_trade_id,
            market_id=key,
            token_id=token,
            question=ls_question,
            direction=ls_dir,
            price=price,
            size=round(bet_size, 2),
        )
        self._spread_positions.add(key)
        self._last_second_trades += 1

    async def _get_fee_rate(self, token_id: str) -> float:
        """Get dynamic taker fee rate for a token. Cached for 5 minutes."""
        cached = self._fee_cache.get(token_id)
        if cached and (_time.monotonic() - cached[1]) < 300:
            return cached[0]

        try:
            resp = await self._http.get(
                f"{CLOB_API}/fee-rate",
                params={"tokenID": token_id},
            )
            if resp.status_code == 200:
                data = resp.json()
                # CLOB API returns base_fee in basis points (bps)
                if "base_fee" in data:
                    rate = float(data["base_fee"]) / 10000  # bps -> fraction
                else:
                    rate = float(data.get("feeRate", data.get("fee_rate", DEFAULT_TAKER_FEE_RATE)))
                self._fee_cache[token_id] = (rate, _time.monotonic())
                return rate
        except Exception:
            pass

        return DEFAULT_TAKER_FEE_RATE

    async def _find_market(self, slug_prefix: str, interval_sec: int) -> MarketPair | None:
        now = int(_time.time())
        boundary = now - (now % interval_sec)

        for offset in range(0, 3):
            ts = boundary + offset * interval_sec
            slug = f"{slug_prefix}-{ts}"
            try:
                resp = await self._http.get(f"{GAMMA_API}/events", params={"slug": slug})
                if resp.status_code != 200:
                    continue
                events = resp.json()
                if not events:
                    continue
                event = events[0]
                markets = event.get("markets", [])
                if not markets:
                    continue
                market = MarketPair(event, markets[0])
                if market.seconds_left > 3:
                    return market
            except Exception:
                continue
        return None

    def status(self) -> dict:
        return {
            "running": self._running,
            "cycles": self._cycle_count,
            "spreads_captured": self._spreads_captured,
            "last_second_trades": self._last_second_trades,
            "active_spreads": len(self._spread_positions),
        }
