import asyncio
from datetime import datetime

import structlog

from analysis.calibration_tracker import CalibrationTracker
from analysis.edge_detector import EdgeDetector
from analysis.market_filter import MarketFilter
from analysis.probability_estimator import EnsembleProbabilityEstimator
from bot.health import HealthChecker
from config.settings import Settings
from core.event_bus import EventBus
from data.base import NewsSource
from data.polymarket_rest import PolymarketClient
from data.polymarket_ws import MarketWebSocket
from notifications.telegram import TelegramNotifier
from storage.repository import MarketRepository
from trading.executor import OrderExecutor
from trading.kelly import KellyCalculator
from trading.portfolio import Portfolio
from trading.risk_manager import RiskManager, TradeProposal

log = structlog.get_logger()

# How often (in cycles) to run a full health check and alert on degradation.
_HEALTH_CHECK_EVERY = 10

# Maximum entries in the analysis cache before cleanup.
_MAX_CACHE_ENTRIES = 200


class TradingPipeline:
    def __init__(
        self,
        settings: Settings,
        polymarket: PolymarketClient,
        ws: MarketWebSocket,
        news_sources: list[NewsSource],
        estimator: EnsembleProbabilityEstimator,
        edge_detector: EdgeDetector,
        kelly: KellyCalculator,
        risk_manager: RiskManager,
        executor: OrderExecutor,
        portfolio: Portfolio,
        calibration: CalibrationTracker,
        telegram: TelegramNotifier,
        market_filter: MarketFilter,
        autoresearch=None,
        event_bus: EventBus | None = None,
        market_repo: MarketRepository | None = None,
        health_checker: HealthChecker | None = None,
    ) -> None:
        self._settings = settings
        self._polymarket = polymarket
        self._ws = ws
        self._news_sources = news_sources
        self._estimator = estimator
        self._edge_detector = edge_detector
        self._kelly = kelly
        self._risk_manager = risk_manager
        self._executor = executor
        self._portfolio = portfolio
        self._calibration = calibration
        self._telegram = telegram
        self._market_filter = market_filter
        self._autoresearch = autoresearch
        self._event_bus = event_bus
        self._market_repo = market_repo
        self._health_checker = health_checker
        self._cycle_count = 0
        self._paused = False  # P3 fix: checked in run_cycle
        # Cache: market_id -> (timestamp, news_hash) to avoid re-analyzing unchanged markets
        self._analysis_cache: dict[str, tuple[float, str]] = {}
        self._cache_ttl = 600  # 10 minutes
        # Last known health state for change detection and dashboard exposure
        self._last_health: dict = {}

    async def run_cycle(self) -> None:
        # P3 fix: respect pause state
        if self._paused:
            log.debug("cycle_skipped_paused")
            return

        self._cycle_count += 1
        log.info("cycle_start", cycle=self._cycle_count)

        try:
            # 0. Sync ALL component thresholds — picks up AutoResearch changes
            self._risk_manager.sync_thresholds(self._settings)
            self._estimator.update_thresholds(
                min_edge=self._settings.min_edge,
                min_confidence=self._settings.min_confidence,
                bias_correction=self._settings.llm_bias_correction,
            )
            self._edge_detector.update_thresholds(
                min_edge=self._settings.min_edge,
                min_confidence=self._settings.min_confidence,
            )

            # 0.5. Clean expired cache entries
            self._cleanup_cache()

            # 1. SCAN: Get candidate markets
            raw_markets = await self._polymarket.get_active_markets(
                min_volume=self._settings.min_market_volume,
                max_days_to_resolution=self._settings.max_days_to_resolution,
            )

            # Cache market data for analytics / meta-strategy research
            if self._market_repo and raw_markets:
                await self._cache_markets(raw_markets)

            pending_market_ids = set()
            if self._executor is not None:
                pending_market_ids = self._executor.pending_market_ids

            candidates = self._market_filter.filter(
                raw_markets,
                held_market_ids=self._portfolio.held_market_ids | pending_market_ids,
            )

            if not candidates:
                log.info("no_candidates", cycle=self._cycle_count)
                await self._check_exits()
            else:
                log.info("candidates_found", count=len(candidates))

                # 2-7. For each candidate: news -> analyze -> edge -> size -> risk -> execute
                for market in candidates:
                    try:
                        await self._process_market(market)
                    except Exception:
                        log.exception("market_processing_error", market=market.question[:50])
                        continue

                # 8. Check exits on existing positions
                await self._check_exits()

            # 9. Persist daily stats to DB
            await self._portfolio.save_daily_stats()

            # 10. Periodic status report (every 5 cycles)
            if self._cycle_count % 5 == 0:
                await self._telegram.send_status_report(self._portfolio.summary())

            # 11. Periodic health check — alert if a source goes down
            if self._health_checker and self._cycle_count % _HEALTH_CHECK_EVERY == 0:
                await self._run_health_check()

            # 12. Publish cycle_complete event for decoupled consumers
            if self._event_bus:
                await self._event_bus.publish(
                    "cycle_complete",
                    cycle=self._cycle_count,
                    stats=self._portfolio.summary(),
                )

        except Exception:
            log.exception("cycle_error", cycle=self._cycle_count)
            await self._telegram.send_risk_alert(
                "Cycle Error", f"Cycle {self._cycle_count} failed. Check logs."
            )

        # AutoResearch: notify experiment loop after each cycle
        if self._autoresearch:
            try:
                await self._autoresearch.on_cycle_complete(self._cycle_count)
            except Exception:
                log.exception("autoresearch_error", cycle=self._cycle_count)

        log.info("cycle_end", cycle=self._cycle_count)

    async def _process_market(self, market) -> None:
        # 2. COLLECT: Gather news from all sources
        news_items = await self._collect_news(market.question)

        # Proceed even without news — LLM can estimate from the question itself
        # (sports markets, known events, etc.). Lower confidence when no news.

        # 2.5. CACHE CHECK: skip LLM if same news already analyzed recently
        news_hash = self._hash_news(news_items) if news_items else "no_news"
        now = datetime.utcnow().timestamp()
        cached = self._analysis_cache.get(market.condition_id)
        if cached:
            cached_time, cached_hash = cached
            if now - cached_time < self._cache_ttl and cached_hash == news_hash:
                log.debug("cache_hit", market=market.question[:50])
                return

        self._analysis_cache[market.condition_id] = (now, news_hash)

        log.info(
            "analyzing_market",
            market=market.question[:50],
            news_count=len(news_items),
            yes_price=market.yes_price,
        )

        # 3-4. ANALYZE: Ensemble probability estimation + edge detection
        estimate = await self._estimator.estimate(
            question=market.question,
            market_price=market.yes_price,
            end_date=market.end_date,
            news_items=news_items,
            lookback_hours=self._settings.news_lookback_hours,
        )

        if estimate is None:
            log.info("no_estimate", market=market.question[:50])
            return

        # 5. DETECT EDGE — pass spread and volume for better signal quality
        edge_signal = self._edge_detector.detect(
            estimated_prob=estimate.probability,
            market_yes_price=market.yes_price,
            market_no_price=market.no_price,
            confidence=estimate.confidence,
            yes_token_id=market.yes_token_id,
            no_token_id=market.no_token_id,
            market_volume=market.volume,
            spread=market.spread,
        )

        if edge_signal is None:
            return

        # 6. SIZE: Kelly criterion
        kelly_fraction = self._settings.get_kelly_fraction(self._portfolio.bankroll)
        kelly_result = self._kelly.calculate(
            estimated_prob=edge_signal.estimated_prob,
            market_price=edge_signal.buy_price,
            bankroll=self._portfolio.bankroll,
            kelly_fraction=kelly_fraction,
        )

        if kelly_result is None:
            log.debug("kelly_skip", market=market.question[:50])
            return

        # 7. RISK CHECK — pass open positions for category exposure check
        proposal = TradeProposal(
            market_id=market.condition_id,
            token_id=edge_signal.token_id,
            question=market.question,
            direction=edge_signal.direction.value,
            price=edge_signal.buy_price,
            size=kelly_result.bet_size,
            edge=edge_signal.edge_size,
            estimated_prob=estimate.probability,
            confidence=estimate.confidence,
            kelly_fraction=kelly_result.bet_fraction,
            reasoning=estimate.reasoning,
        )

        pending_market_ids = set()
        pending_orders_count = 0
        if self._executor is not None:
            pending_market_ids = self._executor.pending_market_ids
            pending_orders_count = len(self._executor.pending_orders)

        approval = await self._risk_manager.approve(
            proposal=proposal,
            bankroll=self._portfolio.bankroll,
            peak_bankroll=self._portfolio.peak_bankroll,
            daily_pnl=self._portfolio.daily_pnl,
            open_positions_count=self._portfolio.open_positions_count + pending_orders_count,
            held_market_ids=self._portfolio.held_market_ids | pending_market_ids,
            market_volume=market.volume,
            open_positions=list(self._portfolio.open_positions),
        )

        if not approval.approved:
            return

        # 8. EXECUTE
        result = await self._executor.execute(proposal)

        if result.success and result.fill_price > 0 and result.fill_size > 0:
            # Update portfolio
            self._portfolio.open_position(
                trade_id=result.trade_id,
                market_id=market.condition_id,
                token_id=edge_signal.token_id,
                question=market.question,
                direction=edge_signal.direction.value,
                price=result.fill_price,
                size=result.fill_size,
            )

            # Record prediction for calibration
            await self._calibration.record_prediction(
                market_id=market.condition_id,
                question=market.question,
                estimated_prob=estimate.probability,
                market_price=market.yes_price,
                confidence=estimate.confidence,
                model_used=",".join(estimate.models_used),
                reasoning=estimate.reasoning,
            )

            # Subscribe to WebSocket for price updates
            await self._ws.subscribe([edge_signal.token_id])

            # Notify via Telegram
            await self._telegram.send_trade_alert({
                "question": market.question,
                "direction": edge_signal.direction.value,
                "price": result.fill_price,
                "size": result.fill_size,
                "edge": edge_signal.edge_size,
                "confidence": estimate.confidence,
                "kelly_fraction": kelly_result.bet_fraction,
                "reasoning": estimate.reasoning,
                "paper_mode": result.paper_mode,
            })

            # Publish trade event for decoupled consumers
            if self._event_bus:
                await self._event_bus.publish(
                    "trade_executed",
                    trade_id=result.trade_id,
                    order_id=result.order_id,
                    market_id=market.condition_id,
                    question=market.question,
                    direction=edge_signal.direction.value,
                    fill_price=result.fill_price,
                    fill_size=result.fill_size,
                    paper_mode=result.paper_mode,
                )

    async def _collect_news(self, query: str) -> list:
        """Collect news from all healthy sources in parallel."""
        tasks = []
        for source in self._news_sources:
            tasks.append(self._safe_fetch_news(source, query))

        results = await asyncio.gather(*tasks)

        all_items = []
        for items in results:
            all_items.extend(items)

        # Deduplicate by normalized title (lowercase, strip punctuation, first 80 chars)
        import re as _re
        seen_titles = set()
        unique = []
        for item in all_items:
            normalized = _re.sub(r"[^\w\s]", "", item.title.lower().strip())[:80]
            if normalized and normalized not in seen_titles:
                seen_titles.add(normalized)
                unique.append(item)

        # Sort by publication time, newest first
        unique.sort(key=lambda x: x.published_at, reverse=True)
        return unique[:20]  # Max 20 items to send to LLM

    async def _safe_fetch_news(self, source: NewsSource, query: str) -> list:
        try:
            return await source.fetch_news(query, self._settings.news_lookback_hours)
        except Exception:
            log.exception("news_fetch_error", source=source.name)
            return []

    async def _check_exits(self) -> None:
        """Check if any open positions should be closed."""
        for pos in list(self._portfolio.open_positions):
            # Get current price: WS cache → CLOB midpoint (public REST) → Gamma API
            current_price = self._ws.get_price(pos.token_id)
            if current_price is None:
                # get_midpoint now has public REST fallback — works in paper mode
                current_price = await self._polymarket.get_midpoint(pos.token_id)
            if current_price is None:
                gamma_yes_price = await self._polymarket.get_market_price_gamma(pos.market_id)
                if gamma_yes_price is not None:
                    is_no_position = "NO" in (pos.direction or "").upper()
                    current_price = 1 - gamma_yes_price if is_no_position else gamma_yes_price

            # If still no price, close positions that are old enough.
            # Without price data we can't manage risk, so free capital after 2h.
            if current_price is None:
                if pos.opened_at:
                    age_hours = (datetime.utcnow() - pos.opened_at).total_seconds() / 3600
                    if age_hours > 2:
                        reason = "no_price_data"
                        log.warning(
                            reason,
                            market=pos.question[:50],
                            age_hours=round(age_hours, 1),
                        )
                        pnl = await self._portfolio.close_position(
                            pos.market_id, pos.entry_price
                        )
                        # Resolve calibration with entry_price as best guess
                        await self._calibration.resolve_by_market_id(
                            pos.market_id, pos.entry_price
                        )
                        if self._event_bus:
                            await self._event_bus.publish(
                                "position_closed",
                                market_id=pos.market_id,
                                reason=reason,
                                pnl=pnl,
                                current_price=pos.entry_price,
                            )
                continue

            self._portfolio.update_position_price(pos.market_id, current_price)

            exit_reason: str | None = None
            actual_outcome: float | None = None

            # 1. Price moved to near resolution (>0.95 or <0.05)
            if current_price > 0.95:
                exit_reason = "near_resolution_yes"
                actual_outcome = 1.0
            elif current_price < 0.05:
                exit_reason = "near_resolution_no"
                actual_outcome = 0.0

            # 2. Take profit: price moved 15% in our favour
            elif pos.direction == "BUY_YES" and current_price >= pos.entry_price * 1.15:
                exit_reason = "take_profit"
                # Price moved strongly toward YES — infer likely outcome
                actual_outcome = 1.0 if current_price > 0.70 else current_price
            elif pos.direction == "BUY_NO" and current_price <= pos.entry_price * 0.85:
                exit_reason = "take_profit"
                actual_outcome = 0.0 if current_price < 0.30 else current_price

            # 3. Stop loss: position lost >30%
            elif pos.pnl_pct < -0.30:
                exit_reason = "stop_loss"
                # Price moved against us — infer outcome from current price
                if pos.direction == "BUY_YES":
                    actual_outcome = 0.0 if current_price < 0.30 else current_price
                else:
                    actual_outcome = 1.0 if current_price > 0.70 else current_price

            # 4. Edge evaporated: use time-scaled patience. Short-lived positions get
            #    more time; only close truly stale ones where edge clearly didn't materialize.
            #    - After 4h with <3% movement: edge is dead, free capital
            #    - After 6h with <8% movement: stale position, close
            elif pos.opened_at:
                age_hours = (datetime.utcnow() - pos.opened_at).total_seconds() / 3600
                if age_hours > 6 and abs(pos.pnl_pct) < 0.08:
                    exit_reason = "edge_evaporated"
                    actual_outcome = current_price  # best estimate at exit time
                elif age_hours > 4 and abs(pos.pnl_pct) < 0.03:
                    exit_reason = "edge_evaporated"
                    actual_outcome = current_price

            if exit_reason:
                pnl = await self._portfolio.close_position(pos.market_id, current_price)
                log.info(exit_reason, market=pos.question[:50], pnl=pnl)

                # Resolve calibration predictions when outcome is known
                if actual_outcome is not None:
                    await self._calibration.resolve_by_market_id(pos.market_id, actual_outcome)

                # Publish exit event
                if self._event_bus:
                    await self._event_bus.publish(
                        "position_closed",
                        market_id=pos.market_id,
                        reason=exit_reason,
                        pnl=pnl,
                        current_price=current_price,
                    )

    async def _cache_markets(self, raw_markets: list) -> None:
        """Persist market data to DB for analytics and meta-strategy research."""
        try:
            for m in raw_markets[:50]:  # Limit to avoid slow cycles
                await self._market_repo.upsert_market(
                    condition_id=m.condition_id,
                    question=m.question,
                    end_date=m.end_date,
                    volume=m.volume,
                    yes_token_id=m.yes_token_id,
                    no_token_id=m.no_token_id,
                )
        except Exception:
            log.exception("market_cache_error")

    async def _run_health_check(self) -> None:
        """Periodic health check — alerts Telegram when a previously healthy source goes down."""
        try:
            prev_healthy = self._last_health.get("healthy", True)
            result = await self._health_checker.check_all()
            self._last_health = result

            if not result["healthy"] and prev_healthy:
                down_sources = [
                    name for name, ok in result.get("news_sources", {}).items() if not ok
                ]
                parts = []
                if not result["polymarket_gamma"]:
                    parts.append("Polymarket API DOWN")
                if down_sources:
                    parts.append(f"News down: {', '.join(down_sources)}")
                await self._telegram.send_risk_alert("Health Degraded", " | ".join(parts))
            elif result["healthy"] and not prev_healthy:
                await self._telegram.send_risk_alert("Health Restored", "All systems healthy.")

        except Exception:
            log.exception("pipeline_health_check_error")

    def _cleanup_cache(self) -> None:
        """Remove expired cache entries to prevent unbounded memory growth."""
        if len(self._analysis_cache) <= _MAX_CACHE_ENTRIES:
            return

        now = datetime.utcnow().timestamp()
        expired_keys = [
            k for k, (ts, _) in self._analysis_cache.items()
            if now - ts > self._cache_ttl
        ]
        for k in expired_keys:
            del self._analysis_cache[k]

        # If still too large after TTL cleanup, evict oldest entries
        if len(self._analysis_cache) > _MAX_CACHE_ENTRIES:
            sorted_keys = sorted(self._analysis_cache, key=lambda k: self._analysis_cache[k][0])
            for k in sorted_keys[:len(self._analysis_cache) - _MAX_CACHE_ENTRIES]:
                del self._analysis_cache[k]

    async def on_gtc_fill(self, trade_id: int, fill_price: float, fill_size: float, proposal_data: dict) -> None:
        """Called by executor when a GTC order is actually filled."""
        if fill_price <= 0 or fill_size <= 0:
            return

        self._portfolio.open_position(
            trade_id=trade_id,
            market_id=proposal_data["market_id"],
            token_id=proposal_data["token_id"],
            question=proposal_data["question"],
            direction=proposal_data["direction"],
            price=fill_price,
            size=fill_size,
        )

        await self._calibration.record_prediction(
            market_id=proposal_data["market_id"],
            question=proposal_data["question"],
            estimated_prob=proposal_data["estimated_prob"],
            market_price=proposal_data["market_price"],
            confidence=proposal_data["confidence"],
            model_used="gtc_deferred",
            reasoning=proposal_data.get("reasoning", ""),
        )

        await self._ws.subscribe([proposal_data["token_id"]])

        await self._telegram.send_trade_alert({
            "question": proposal_data["question"],
            "direction": proposal_data["direction"],
            "price": fill_price,
            "size": fill_size,
            "edge": proposal_data["edge"],
            "confidence": proposal_data["confidence"],
            "kelly_fraction": proposal_data["kelly_fraction"],
            "reasoning": proposal_data.get("reasoning", ""),
            "paper_mode": False,
        })

        log.info("gtc_fill_position_opened", trade_id=trade_id, market=proposal_data["question"][:50])

    @property
    def last_health(self) -> dict:
        """Last known health check results — exposed to web dashboard."""
        return self._last_health

    @staticmethod
    def _hash_news(news_items: list) -> str:
        """Hash of all news titles + count to detect any change in news set."""
        import hashlib
        titles = "|".join(sorted(item.title[:80] for item in news_items))
        content = f"{len(news_items)}:{titles}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
