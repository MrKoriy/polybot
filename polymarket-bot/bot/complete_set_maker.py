"""Conservative complete-set market-making scanner.

The scanner never assumes passive fills and never submits orders.  It reads the
public Gamma/CLOB APIs and builds post-only quote plans for both outcome tokens
of a binary market.  If both BUY quotes fill, the resulting YES+NO complete set
can be merged back into one dollar of collateral.

This module is intentionally independent from the live executor.  It is safe to
run in shadow mode while the live integration is migrated to CLOB V2.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx
import structlog

from data.polymarket_rest import PolymarketClient

log = structlog.get_logger()

SPORT_TAGS = {
    "Sports",
    "Soccer",
    "NBA",
    "NHL",
    "NFL",
    "MLB",
    "Tennis",
    "Esports",
}


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


@dataclass(frozen=True)
class MakerScannerConfig:
    quote_shares: float = 5.0
    min_gross_edge: float = 0.006
    max_emergency_loss_usd: float = 0.15
    max_market_reserve_pct: float = 0.10
    max_total_reserve_pct: float = 0.50
    max_markets: int = 8
    max_resolution_hours: float = 72.0
    min_volume_24h: float = 2_000.0
    event_pages: int = 2
    event_page_size: int = 100
    scan_interval_seconds: int = 60
    request_concurrency: int = 12
    request_timeout_seconds: float = 8.0
    max_candidates_per_scan: int = 60


@dataclass(frozen=True)
class FeeSchedule:
    rate: float = 0.0
    exponent: float = 1.0
    rebate_rate: float = 0.0

    @classmethod
    def from_market(cls, market: dict) -> "FeeSchedule":
        raw = market.get("feeSchedule") or {}
        return cls(
            rate=max(0.0, _safe_float(raw.get("rate"))),
            exponent=max(1.0, _safe_float(raw.get("exponent"), 1.0)),
            rebate_rate=max(0.0, _safe_float(raw.get("rebateRate"))),
        )

    def taker_fee(self, shares: float, price: float) -> float:
        """Return the V2 dynamic taker fee in USDC.

        CLOB V2 exposes a per-market rate and curve exponent.  Makers pay no
        trading fee; this value is used only for emergency taker hedges and for
        estimating the rebate funded by the counterparty's taker fee.
        """
        if shares <= 0 or not 0 < price < 1 or self.rate <= 0:
            return 0.0
        curve = (price * (1.0 - price)) ** self.exponent
        return shares * self.rate * curve


@dataclass(frozen=True)
class BookTop:
    bid: float
    ask: float
    tick_size: float
    min_order_size: float
    bid_size: float = 0.0
    ask_size: float = 0.0


@dataclass(frozen=True)
class CompleteSetOpportunity:
    condition_id: str
    question: str
    event_title: str
    event_slug: str
    end_date: str
    yes_token_id: str
    no_token_id: str
    yes_outcome: str
    no_outcome: str
    quote_yes: float
    quote_no: float
    shares: float
    pair_cost: float
    gross_edge: float
    locked_edge_usd: float
    expected_rebate_usd: float
    worst_emergency_loss_usd: float
    reserve_usd: float
    volume_24h: float
    liquidity: float
    reward_daily: float
    rewards_min_size: float
    reward_eligible: bool
    score: float

    def to_dict(self) -> dict:
        return asdict(self)


def _top_of_book(book: dict) -> BookTop | None:
    bids = []
    asks = []
    for row in book.get("bids") or []:
        price = _safe_float(row.get("price"))
        size = _safe_float(row.get("size"))
        if 0 < price < 1 and size > 0:
            bids.append((price, size))
    for row in book.get("asks") or []:
        price = _safe_float(row.get("price"))
        size = _safe_float(row.get("size"))
        if 0 < price < 1 and size > 0:
            asks.append((price, size))
    if not bids or not asks:
        return None
    best_bid = max(bids, key=lambda item: item[0])
    best_ask = min(asks, key=lambda item: item[0])
    tick = _safe_float(book.get("tick_size"), 0.01)
    min_order = _safe_float(book.get("min_order_size"), 5.0)
    return BookTop(
        bid=best_bid[0],
        ask=best_ask[0],
        tick_size=tick if tick > 0 else 0.01,
        min_order_size=min_order if min_order > 0 else 5.0,
        bid_size=best_bid[1],
        ask_size=best_ask[1],
    )


def _maker_bid(book: BookTop) -> float:
    """Improve by one tick only when that remains strictly post-only."""
    improved = round(book.bid + book.tick_size, 6)
    if improved < book.ask:
        return improved
    return book.bid


def evaluate_binary_market(
    *,
    market: dict,
    event: dict,
    yes_book: dict,
    no_book: dict,
    bankroll: float,
    config: MakerScannerConfig,
) -> CompleteSetOpportunity | None:
    """Build a capital- and hedge-aware complete-set quote plan."""
    yes_top = _top_of_book(yes_book)
    no_top = _top_of_book(no_book)
    if yes_top is None or no_top is None or bankroll <= 0:
        return None

    quote_yes = _maker_bid(yes_top)
    quote_no = _maker_bid(no_top)
    pair_cost = quote_yes + quote_no
    gross_edge = 1.0 - pair_cost
    if gross_edge + 1e-9 < config.min_gross_edge:
        return None

    shares = max(config.quote_shares, yes_top.min_order_size, no_top.min_order_size)
    fee = FeeSchedule.from_market(market)
    # If only one maker bid fills, immediately buying the complement is the
    # conservative flattening path.  Include the taker fee in that loss.
    yes_filled_cost = quote_yes + no_top.ask
    no_filled_cost = quote_no + yes_top.ask
    yes_hedge_loss = max(
        0.0,
        shares * (yes_filled_cost - 1.0) + fee.taker_fee(shares, no_top.ask),
    )
    no_hedge_loss = max(
        0.0,
        shares * (no_filled_cost - 1.0) + fee.taker_fee(shares, yes_top.ask),
    )
    worst_emergency_loss = max(yes_hedge_loss, no_hedge_loss)
    if worst_emergency_loss > config.max_emergency_loss_usd + 1e-9:
        return None

    # Reserve enough cash for either two passive fills or a one-leg fill plus
    # an immediate taker hedge.  This avoids understating capital use exactly
    # when the market moves against one quote.
    reserve_usd = max(
        shares * pair_cost,
        shares * yes_filled_cost + fee.taker_fee(shares, no_top.ask),
        shares * no_filled_cost + fee.taker_fee(shares, yes_top.ask),
    )
    if reserve_usd > bankroll * config.max_market_reserve_pct + 1e-9:
        return None

    expected_rebate = fee.rebate_rate * (
        fee.taker_fee(shares, quote_yes) + fee.taker_fee(shares, quote_no)
    )
    locked_edge_usd = shares * gross_edge

    reward_daily = sum(
        _safe_float(row.get("rewardsDailyRate"))
        for row in (market.get("clobRewards") or [])
    )
    rewards_min_size = _safe_float(market.get("rewardsMinSize"))
    reward_eligible = reward_daily > 0 and shares + 1e-9 >= rewards_min_size

    tokens = _json_list(market.get("clobTokenIds"))
    outcomes = _json_list(market.get("outcomes"))
    if len(tokens) < 2:
        return None

    # Score locked spread and estimated rebate, while penalizing the full
    # emergency hedge cost.  Reward pools are deliberately not capitalized in
    # the score because the user's pro-rata share is unknown.
    score = locked_edge_usd + expected_rebate - worst_emergency_loss
    return CompleteSetOpportunity(
        condition_id=str(market.get("conditionId") or ""),
        question=str(market.get("question") or ""),
        event_title=str(event.get("title") or ""),
        event_slug=str(event.get("slug") or ""),
        end_date=str(market.get("endDate") or event.get("endDate") or ""),
        yes_token_id=str(tokens[0]),
        no_token_id=str(tokens[1]),
        yes_outcome=str(outcomes[0]) if outcomes else "Yes",
        no_outcome=str(outcomes[1]) if len(outcomes) > 1 else "No",
        quote_yes=quote_yes,
        quote_no=quote_no,
        shares=shares,
        pair_cost=pair_cost,
        gross_edge=gross_edge,
        locked_edge_usd=locked_edge_usd,
        expected_rebate_usd=expected_rebate,
        worst_emergency_loss_usd=worst_emergency_loss,
        reserve_usd=reserve_usd,
        volume_24h=_safe_float(market.get("volume24hr")),
        liquidity=_safe_float(market.get("liquidityNum")),
        reward_daily=reward_daily,
        rewards_min_size=rewards_min_size,
        reward_eligible=reward_eligible,
        score=score,
    )


class CompleteSetScanner:
    def __init__(
        self,
        polymarket: PolymarketClient,
        bankroll_getter: Callable[[], float],
        *,
        gamma_url: str = "https://gamma-api.polymarket.com",
        config: MakerScannerConfig | None = None,
    ) -> None:
        self._polymarket = polymarket
        self._bankroll_getter = bankroll_getter
        self._gamma_url = gamma_url.rstrip("/")
        self.config = config or MakerScannerConfig()
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=self.config.request_timeout_seconds)
        )
        self._book_sem = asyncio.Semaphore(self.config.request_concurrency)

    async def close(self) -> None:
        await self._http.aclose()

    async def _fetch_events(self) -> list[dict]:
        events: list[dict] = []
        for page in range(self.config.event_pages):
            response = await self._http.get(
                f"{self._gamma_url}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": self.config.event_page_size,
                    "offset": page * self.config.event_page_size,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or not payload:
                break
            events.extend(payload)
            if len(payload) < self.config.event_page_size:
                break
        return events

    @staticmethod
    def _is_sport_event(event: dict) -> bool:
        tags = {str(tag.get("label") or "") for tag in (event.get("tags") or [])}
        return bool(tags & SPORT_TAGS)

    def _is_near_term(self, event: dict, market: dict) -> bool:
        raw = market.get("endDate") or event.get("endDate")
        if not raw:
            return False
        try:
            end = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            hours = (end - datetime.now(timezone.utc)).total_seconds() / 3600
            return 0 < hours <= self.config.max_resolution_hours
        except (TypeError, ValueError):
            return False

    async def _get_book(self, token_id: str) -> dict:
        async with self._book_sem:
            return await asyncio.wait_for(
                self._polymarket.get_orderbook(token_id),
                timeout=self.config.request_timeout_seconds,
            )

    async def _evaluate(self, event: dict, market: dict) -> CompleteSetOpportunity | None:
        tokens = _json_list(market.get("clobTokenIds"))
        if len(tokens) < 2:
            return None
        yes_book, no_book = await asyncio.gather(
            self._get_book(str(tokens[0])),
            self._get_book(str(tokens[1])),
        )
        return evaluate_binary_market(
            market=market,
            event=event,
            yes_book=yes_book,
            no_book=no_book,
            bankroll=float(self._bankroll_getter()),
            config=self.config,
        )

    async def scan(self) -> list[CompleteSetOpportunity]:
        events = await self._fetch_events()
        candidates: list[tuple[dict, dict]] = []
        for event in events:
            if not self._is_sport_event(event):
                continue
            for market in event.get("markets") or []:
                if not market.get("active") or market.get("closed"):
                    continue
                if _safe_float(market.get("volume24hr")) < self.config.min_volume_24h:
                    continue
                if not self._is_near_term(event, market):
                    continue
                if len(_json_list(market.get("clobTokenIds"))) < 2:
                    continue
                candidates.append((event, market))

        candidates = candidates[: self.config.max_candidates_per_scan]
        results = await asyncio.gather(
            *(self._evaluate(event, market) for event, market in candidates),
            return_exceptions=True,
        )
        opportunities = [row for row in results if isinstance(row, CompleteSetOpportunity)]
        opportunities.sort(
            key=lambda row: (row.score / max(row.reserve_usd, 0.01), row.volume_24h),
            reverse=True,
        )
        return opportunities

    def allocate(self, opportunities: list[CompleteSetOpportunity]) -> list[CompleteSetOpportunity]:
        bankroll = float(self._bankroll_getter())
        budget = bankroll * self.config.max_total_reserve_pct
        selected: list[CompleteSetOpportunity] = []
        used = 0.0
        seen_events: set[str] = set()
        for row in opportunities:
            # Avoid stacking several correlated markets from the same game.
            event_key = row.event_slug or row.event_title
            if event_key in seen_events:
                continue
            if used + row.reserve_usd > budget + 1e-9:
                continue
            selected.append(row)
            seen_events.add(event_key)
            used += row.reserve_usd
            if len(selected) >= self.config.max_markets:
                break
        return selected


class CompleteSetShadowMaker:
    """Periodic, read-only quote planner for a $100 paper bankroll."""

    def __init__(self, scanner: CompleteSetScanner) -> None:
        self.scanner = scanner
        self._running = False
        self._task: asyncio.Task | None = None
        self._cycle_count = 0
        self._last_error = ""
        self._last_scan_at = ""
        self._opportunities: list[CompleteSetOpportunity] = []
        self._allocated: list[CompleteSetOpportunity] = []

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="complete_set_shadow")
        log.info("complete_set_shadow_started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.scanner.close()
        log.info("complete_set_shadow_stopped", cycles=self._cycle_count)

    async def refresh(self) -> None:
        self._cycle_count += 1
        try:
            self._opportunities = await self.scanner.scan()
            self._allocated = self.scanner.allocate(self._opportunities)
            self._last_scan_at = datetime.now(timezone.utc).isoformat()
            self._last_error = ""
            total_reserve = sum(row.reserve_usd for row in self._allocated)
            total_locked = sum(row.locked_edge_usd for row in self._allocated)
            log.info(
                "complete_set_shadow_scan",
                opportunities=len(self._opportunities),
                allocated=len(self._allocated),
                reserve_usd=round(total_reserve, 4),
                locked_edge_if_both_fill=round(total_locked, 4),
                reward_eligible=sum(row.reward_eligible for row in self._allocated),
            )
            for row in self._allocated[:3]:
                log.info(
                    "complete_set_quote_plan",
                    market=row.question[:70],
                    quote_yes=row.quote_yes,
                    quote_no=row.quote_no,
                    shares=row.shares,
                    gross_edge_pct=round(row.gross_edge * 100, 3),
                    locked_edge_usd=round(row.locked_edge_usd, 4),
                    emergency_loss_usd=round(row.worst_emergency_loss_usd, 4),
                )
        except Exception as exc:
            self._last_error = str(exc)
            log.exception("complete_set_shadow_error")

    async def _run_loop(self) -> None:
        while self._running:
            await self.refresh()
            await asyncio.sleep(self.scanner.config.scan_interval_seconds)

    def status(self) -> dict:
        return {
            "mode": "shadow",
            "cycles": self._cycle_count,
            "last_scan_at": self._last_scan_at,
            "last_error": self._last_error,
            "opportunities": len(self._opportunities),
            "allocated": len(self._allocated),
            "reserve_usd": round(sum(row.reserve_usd for row in self._allocated), 4),
            "locked_edge_if_both_fill_usd": round(
                sum(row.locked_edge_usd for row in self._allocated), 4
            ),
            "quotes": [row.to_dict() for row in self._allocated],
        }
