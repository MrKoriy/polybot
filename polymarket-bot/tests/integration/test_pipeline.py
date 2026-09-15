"""Integration tests for the full trading pipeline.

Tests the complete cycle: scan → collect → analyze → detect edge → size →
risk check → execute (paper mode), exit logic, and E2E simulation.

Uses unittest.mock for all external APIs (LLM, Polymarket REST, WebSocket,
Telegram). Uses an in-memory SQLite database — no real money or network calls.
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from analysis.calibration_tracker import CalibrationTracker
from analysis.edge_detector import EdgeDetector
from analysis.llm_client import ProbabilityEstimate
from analysis.market_filter import MarketFilter
from analysis.probability_estimator import EnsembleEstimate, EnsembleProbabilityEstimator
from bot.pipeline import TradingPipeline
from config.settings import Settings
from core.event_bus import EventBus
from data.base import MarketCandidate, NewsItem, NewsSource
from storage.database import close_db, init_db
from storage.repository import (
    DailyStatRepository,
    MarketRepository,
    PredictionRepository,
    RiskEventRepository,
    TradeRepository,
)
from trading.executor import ExecutionResult, OrderExecutor
from trading.kelly import KellyCalculator
from trading.portfolio import Portfolio
from trading.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_market(
    condition_id: str = "market_001",
    question: str = "Will BTC exceed $100k by end of month?",
    yes_price: float = 0.45,
    no_price: float = 0.55,
    volume: float = 50_000.0,
    volume_24h: float = 10_000.0,
    end_date: str = "",
) -> MarketCandidate:
    if not end_date:
        # 24 hours from now — well within max_hours=48 and max_days=3
        end_date = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    # Spread must stay within MarketFilter.max_spread=0.15
    spread = abs(yes_price - no_price)
    return MarketCandidate(
        condition_id=condition_id,
        question=question,
        yes_token_id=f"{condition_id}_yes",
        no_token_id=f"{condition_id}_no",
        yes_price=yes_price,
        no_price=no_price,
        volume=volume,
        volume_24h=volume_24h,
        end_date=end_date,
        spread=spread,
        category="crypto",
    )


def _make_news(count: int = 3, source: str = "reddit") -> list[NewsItem]:
    now = datetime.now(timezone.utc)
    return [
        NewsItem(
            title=f"Market news item {i}",
            body=f"Relevant market analysis for item {i}",
            source=source,
            url=f"https://example.com/news/{i}",
            published_at=now - timedelta(minutes=i * 10),
        )
        for i in range(count)
    ]


def _make_estimate(
    probability: float = 0.70,
    confidence: float = 0.80,
    model: str = "test-model",
) -> EnsembleEstimate:
    raw = ProbabilityEstimate(
        probability=probability,
        confidence=confidence,
        reasoning="Strong technical signals support this estimate",
        key_factors=["factor1", "factor2"],
        counter_arguments=["counter1"],
        model_used=model,
    )
    return EnsembleEstimate(
        probability=probability,
        confidence=confidence,
        reasoning="Strong technical signals support this estimate",
        key_factors=["factor1", "factor2"],
        counter_arguments=["counter1"],
        models_used=[model],
        raw_estimates=[raw],
    )


class MockNewsSource(NewsSource):
    """Controllable mock news source."""

    def __init__(self, name_: str = "mock", items: list[NewsItem] | None = None, fail: bool = False):
        self._name = name_
        self._items = _make_news() if items is None else items
        self._fail = fail
        self.fetch_calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def fetch_news(self, query: str, lookback_hours: int = 2) -> list[NewsItem]:
        self.fetch_calls += 1
        if self._fail:
            raise RuntimeError("Source unavailable")
        return self._items

    async def health_check(self) -> bool:
        return not self._fail

    async def close(self) -> None:
        pass


@pytest.fixture
async def db(tmp_path):
    """Set up a fresh in-memory SQLite DB for each test."""
    db_path = str(tmp_path / "test.db")
    await init_db(db_path)
    yield db_path
    await close_db()


@pytest.fixture
def settings():
    s = Settings(
        paper_trading=True,
        min_edge=0.08,
        min_confidence=0.70,
        min_market_volume=1_000.0,
        max_days_to_resolution=3,
        max_hours_to_resolution=48,
        top_markets_to_analyze=10,
        kelly_fraction=0.25,
        max_position_pct=0.25,
        max_open_positions_small=3,
        max_open_positions_large=5,
        daily_loss_limit=0.30,
        drawdown_pause=0.50,
        llm_bias_correction=0.04,
        news_lookback_hours=12,
    )
    return s


@pytest.fixture
async def repos(db):
    return {
        "trade": TradeRepository(),
        "prediction": PredictionRepository(),
        "daily_stat": DailyStatRepository(),
        "risk": RiskEventRepository(),
        "market": MarketRepository(),
    }


@pytest.fixture
async def portfolio(repos):
    p = Portfolio(
        initial_bankroll=100.0,
        trade_repo=repos["trade"],
        daily_stat_repo=repos["daily_stat"],
        paper_mode=True,
    )
    await p.sync_from_db()
    return p


def _make_pipeline(settings, portfolio, repos, news_sources=None, estimate=None):
    """Build a TradingPipeline with mocked externals."""
    mock_polymarket = MagicMock()
    mock_polymarket.get_active_markets = AsyncMock(return_value=[])
    mock_polymarket.get_midpoint = AsyncMock(return_value=None)
    mock_polymarket.get_market_price_gamma = AsyncMock(return_value=None)

    mock_ws = MagicMock()
    mock_ws.get_price = MagicMock(return_value=None)
    mock_ws.subscribe = AsyncMock()
    mock_ws.start = AsyncMock()
    mock_ws.stop = AsyncMock()

    mock_telegram = MagicMock()
    mock_telegram.send = AsyncMock()
    mock_telegram.send_trade_alert = AsyncMock()
    mock_telegram.send_risk_alert = AsyncMock()
    mock_telegram.send_status_report = AsyncMock()

    mock_estimator = MagicMock(spec=EnsembleProbabilityEstimator)
    mock_estimator.estimate = AsyncMock(return_value=estimate)
    mock_estimator.update_thresholds = MagicMock()

    executor = OrderExecutor(
        polymarket=mock_polymarket,
        trade_repo=repos["trade"],
        paper_mode=True,
    )

    risk_manager = RiskManager(
        max_position_pct=settings.max_position_pct,
        max_open_positions_small=settings.max_open_positions_small,
        max_open_positions_large=settings.max_open_positions_large,
        daily_loss_limit=settings.daily_loss_limit,
        drawdown_pause=settings.drawdown_pause,
        min_edge=settings.min_edge,
        min_confidence=settings.min_confidence,
        min_market_volume=settings.min_market_volume,
        risk_repo=repos["risk"],
    )

    pipeline = TradingPipeline(
        settings=settings,
        polymarket=mock_polymarket,
        ws=mock_ws,
        news_sources=news_sources or [MockNewsSource()],
        estimator=mock_estimator,
        edge_detector=EdgeDetector(
            min_edge=settings.min_edge,
            min_confidence=settings.min_confidence,
        ),
        kelly=KellyCalculator(
            default_fraction=settings.kelly_fraction,
            max_position_pct=settings.max_position_pct,
        ),
        risk_manager=risk_manager,
        executor=executor,
        portfolio=portfolio,
        calibration=CalibrationTracker(repos["prediction"]),
        telegram=mock_telegram,
        market_filter=MarketFilter(
            min_volume=settings.min_market_volume,
            max_days_to_resolution=settings.max_days_to_resolution,
            max_hours_to_resolution=settings.max_hours_to_resolution,
            top_n=settings.top_markets_to_analyze,
        ),
        event_bus=EventBus(),
        market_repo=repos["market"],
    )

    return pipeline, mock_polymarket, mock_ws, mock_telegram, mock_estimator


# ---------------------------------------------------------------------------
# 1. Full pipeline cycle: edge detected → paper trade executed
# ---------------------------------------------------------------------------

class TestFullPipelineCycle:
    async def test_edge_detected_executes_paper_trade(self, db, settings, repos, portfolio):
        market = _make_market(yes_price=0.45)
        estimate = _make_estimate(probability=0.70, confidence=0.82)  # 25% above market → edge

        pipeline, mock_poly, mock_ws, mock_tg, mock_est = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )
        mock_poly.get_active_markets.return_value = [market]

        initial_bankroll = portfolio.bankroll
        await pipeline.run_cycle()

        # Position should be opened
        assert portfolio.open_positions_count == 1
        pos = portfolio.open_positions[0]
        assert pos.market_id == "market_001"
        assert pos.direction == "BUY_YES"
        assert pos.size > 0

        # Bankroll should be reduced
        assert portfolio.bankroll < initial_bankroll

        # Telegram trade alert should be sent
        mock_tg.send_trade_alert.assert_called_once()

        # DB should have the trade
        trades = await repos["trade"].get_open_trades(paper_mode=True)
        assert len(trades) == 1
        assert trades[0].market_id == "market_001"

    async def test_no_edge_no_trade(self, db, settings, repos, portfolio):
        """When LLM estimate is close to market price, no trade should be taken."""
        # Tight spread market
        market = _make_market(yes_price=0.48, no_price=0.52)
        # estimate close to market price — no edge after bias correction
        estimate = _make_estimate(probability=0.50, confidence=0.82)

        pipeline, mock_poly, _, mock_tg, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )
        mock_poly.get_active_markets.return_value = [market]

        await pipeline.run_cycle()

        assert portfolio.open_positions_count == 0
        mock_tg.send_trade_alert.assert_not_called()

    async def test_no_candidates_no_trade(self, db, settings, repos, portfolio):
        """When no markets pass the filter, nothing should happen."""
        pipeline, mock_poly, _, mock_tg, _ = _make_pipeline(
            settings, portfolio, repos, estimate=None
        )
        mock_poly.get_active_markets.return_value = []

        await pipeline.run_cycle()

        assert portfolio.open_positions_count == 0
        mock_tg.send_trade_alert.assert_not_called()

    async def test_llm_returns_none_no_trade(self, db, settings, repos, portfolio):
        """When LLM returns None (no edge from LLM), no trade is taken."""
        market = _make_market(yes_price=0.45, no_price=0.55)

        pipeline, mock_poly, _, mock_tg, _ = _make_pipeline(
            settings, portfolio, repos, estimate=None
        )
        mock_poly.get_active_markets.return_value = [market]

        await pipeline.run_cycle()

        assert portfolio.open_positions_count == 0
        mock_tg.send_trade_alert.assert_not_called()

    async def test_buy_no_edge(self, db, settings, repos, portfolio):
        """When LLM probability is much lower than market price, BUY_NO trade taken."""
        # Market says 57% YES, LLM says 35% — strong NO edge; spread=0.14 passes filter
        market = _make_market(yes_price=0.57, no_price=0.43)
        estimate = _make_estimate(probability=0.35, confidence=0.82)

        pipeline, mock_poly, _, mock_tg, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )
        mock_poly.get_active_markets.return_value = [market]

        await pipeline.run_cycle()

        assert portfolio.open_positions_count == 1
        assert portfolio.open_positions[0].direction == "BUY_NO"
        mock_tg.send_trade_alert.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Exit logic
# ---------------------------------------------------------------------------

class TestExitLogic:
    async def test_exit_near_resolution_yes(self, db, settings, repos, portfolio):
        """Position closes when price exceeds 0.95 (near YES resolution)."""
        market = _make_market(condition_id="exit_test", yes_price=0.45, no_price=0.55)
        estimate = _make_estimate(probability=0.70, confidence=0.82)

        pipeline, mock_poly, mock_ws, _, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )
        mock_poly.get_active_markets.return_value = [market]

        # Cycle 1: open position
        await pipeline.run_cycle()
        assert portfolio.open_positions_count == 1

        # Cycle 2: price spikes to 0.97 → near resolution YES
        mock_poly.get_active_markets.return_value = []
        mock_ws.get_price.return_value = 0.97

        pnl_before = portfolio.daily_pnl
        await pipeline.run_cycle()

        assert portfolio.open_positions_count == 0
        assert portfolio.daily_pnl > pnl_before  # Profitable exit

        # Prediction should be resolved with outcome=1.0
        from storage.repository import PredictionRepository
        preds = await repos["prediction"].get_resolved_predictions(limit=10)
        assert len(preds) == 1
        assert preds[0].actual_outcome == 1.0

    async def test_exit_near_resolution_no(self, db, settings, repos, portfolio):
        """Position closes when price drops below 0.05 (near NO resolution)."""
        # BUY_NO: market says 57% YES, LLM says 35%; spread=0.14
        market = _make_market(condition_id="no_exit", yes_price=0.57, no_price=0.43)
        estimate = _make_estimate(probability=0.35, confidence=0.82)

        pipeline, mock_poly, mock_ws, _, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )
        mock_poly.get_active_markets.return_value = [market]
        await pipeline.run_cycle()
        assert portfolio.open_positions_count == 1

        # Price drops near 0 on YES (NO resolves TRUE)
        mock_poly.get_active_markets.return_value = []
        mock_ws.get_price.return_value = 0.03

        await pipeline.run_cycle()
        assert portfolio.open_positions_count == 0

    async def test_exit_take_profit(self, db, settings, repos, portfolio):
        """BUY_YES position closes when price rises 15%+ from entry."""
        market = _make_market(condition_id="tp_test", yes_price=0.45, no_price=0.55)
        estimate = _make_estimate(probability=0.70, confidence=0.82)

        pipeline, mock_poly, mock_ws, _, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )
        mock_poly.get_active_markets.return_value = [market]
        await pipeline.run_cycle()

        pos = portfolio.open_positions[0]
        entry = pos.entry_price
        mock_poly.get_active_markets.return_value = []
        # 16% above entry triggers take_profit
        mock_ws.get_price.return_value = round(entry * 1.16, 4)

        await pipeline.run_cycle()
        assert portfolio.open_positions_count == 0

    async def test_exit_stop_loss(self, db, settings, repos, portfolio):
        """Position closes when P&L drops below -30%."""
        market = _make_market(condition_id="sl_test", yes_price=0.45, no_price=0.55)
        estimate = _make_estimate(probability=0.70, confidence=0.82)

        pipeline, mock_poly, mock_ws, _, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )
        mock_poly.get_active_markets.return_value = [market]
        await pipeline.run_cycle()

        pos = portfolio.open_positions[0]
        entry = pos.entry_price
        mock_poly.get_active_markets.return_value = []
        # Drop 35% below entry → stop loss triggers
        mock_ws.get_price.return_value = round(entry * 0.65, 4)

        await pipeline.run_cycle()
        assert portfolio.open_positions_count == 0


# ---------------------------------------------------------------------------
# 3. Risk manager blocking
# ---------------------------------------------------------------------------

class TestRiskManagerBlocking:
    async def test_blocks_when_max_positions_reached(self, db, settings, repos):
        """No new trade when max open positions (small bankroll) reached."""
        portfolio = Portfolio(
            initial_bankroll=30.0,  # < $50 → max 3 positions (small)
            trade_repo=repos["trade"],
            daily_stat_repo=repos["daily_stat"],
            paper_mode=True,
        )
        await portfolio.sync_from_db()

        estimate = _make_estimate(probability=0.70, confidence=0.82)
        pipeline, mock_poly, _, mock_tg, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )

        # Open 3 positions manually by running 3 cycles with distinct markets
        for i in range(3):
            mock_poly.get_active_markets.return_value = [
                _make_market(condition_id=f"mkt_{i}", yes_price=0.45, no_price=0.55)
            ]
            await pipeline.run_cycle()

        assert portfolio.open_positions_count == 3

        # 4th market should be blocked
        mock_poly.get_active_markets.return_value = [
            _make_market(condition_id="mkt_blocked", yes_price=0.45, no_price=0.55)
        ]
        mock_tg.send_trade_alert.reset_mock()
        await pipeline.run_cycle()

        assert portfolio.open_positions_count == 3  # No new position
        mock_tg.send_trade_alert.assert_not_called()

    async def test_blocks_on_daily_loss_limit(self, db, settings, repos):
        """No new trade when daily loss limit exceeded."""
        portfolio = Portfolio(
            initial_bankroll=100.0,
            trade_repo=repos["trade"],
            daily_stat_repo=repos["daily_stat"],
            paper_mode=True,
        )
        await portfolio.sync_from_db()

        # Inject a large daily loss directly
        portfolio._daily_pnl = -35.0  # -35% of $100 bankroll

        estimate = _make_estimate(probability=0.70, confidence=0.82)
        pipeline, mock_poly, _, mock_tg, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )
        mock_poly.get_active_markets.return_value = [_make_market(yes_price=0.45, no_price=0.55)]

        await pipeline.run_cycle()

        assert portfolio.open_positions_count == 0
        mock_tg.send_trade_alert.assert_not_called()

    async def test_blocks_duplicate_market(self, db, settings, repos, portfolio):
        """Cannot open a second position on the same market."""
        estimate = _make_estimate(probability=0.70, confidence=0.82)
        pipeline, mock_poly, _, mock_tg, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )
        market = _make_market(condition_id="dup_market", yes_price=0.45, no_price=0.55)
        mock_poly.get_active_markets.return_value = [market]

        # First cycle opens position
        await pipeline.run_cycle()
        assert portfolio.open_positions_count == 1

        # Second cycle — same market comes up again
        mock_tg.send_trade_alert.reset_mock()
        await pipeline.run_cycle()
        assert portfolio.open_positions_count == 1  # still 1
        mock_tg.send_trade_alert.assert_not_called()


# ---------------------------------------------------------------------------
# 4. News collection resilience
# ---------------------------------------------------------------------------

class TestNewsCollection:
    async def test_failing_source_does_not_break_pipeline(self, db, settings, repos, portfolio):
        """A failing news source is silently skipped; other sources still work."""
        good_source = MockNewsSource("good", _make_news(3))
        bad_source = MockNewsSource("bad", fail=True)
        estimate = _make_estimate(probability=0.70, confidence=0.82)

        pipeline, mock_poly, _, _, _ = _make_pipeline(
            settings, portfolio, repos,
            news_sources=[good_source, bad_source],
            estimate=estimate,
        )
        mock_poly.get_active_markets.return_value = [_make_market(yes_price=0.45, no_price=0.55)]

        # Should not raise
        await pipeline.run_cycle()

        # Good source was queried
        assert good_source.fetch_calls == 1
        # Bad source was attempted
        assert bad_source.fetch_calls == 1

    async def test_no_news_still_calls_llm(self, db, settings, repos, portfolio):
        """Even without news, LLM is called — it can estimate from the question itself."""
        empty_source = MockNewsSource("empty", items=[])
        estimate = _make_estimate(probability=0.70, confidence=0.82)

        pipeline, mock_poly, _, mock_tg, mock_est = _make_pipeline(
            settings, portfolio, repos,
            news_sources=[empty_source],
            estimate=estimate,
        )
        mock_poly.get_active_markets.return_value = [_make_market(yes_price=0.45, no_price=0.55)]

        await pipeline.run_cycle()

        # LLM IS called even without news (sports markets, known events, etc.)
        mock_est.estimate.assert_called_once()

    async def test_multiple_sources_deduplicated(self, db, settings, repos, portfolio):
        """Duplicate news titles from multiple sources are deduplicated."""
        same_news = _make_news(2, source="src_a")
        source_a = MockNewsSource("src_a", items=same_news)
        source_b = MockNewsSource("src_b", items=same_news)  # Same titles
        estimate = _make_estimate(probability=0.70, confidence=0.82)

        pipeline, mock_poly, _, _, mock_est = _make_pipeline(
            settings, portfolio, repos,
            news_sources=[source_a, source_b],
            estimate=estimate,
        )
        mock_poly.get_active_markets.return_value = [_make_market(yes_price=0.45, no_price=0.55)]

        await pipeline.run_cycle()

        # LLM called with deduplicated news (not double the items)
        mock_est.estimate.assert_called_once()
        call_args = mock_est.estimate.call_args
        news_passed = call_args.kwargs.get("news_items") or call_args.args[3]
        # Should be 2 unique items, not 4
        assert len(news_passed) == 2


# ---------------------------------------------------------------------------
# 5. E2E paper trading simulation: 10+ market cycles
# ---------------------------------------------------------------------------

class TestE2EPaperTradingSimulation:
    async def test_10_cycles_paper_trading(self, db, settings, repos):
        """Run 10 trading cycles in paper mode, verify state consistency."""
        portfolio = Portfolio(
            initial_bankroll=100.0,
            trade_repo=repos["trade"],
            daily_stat_repo=repos["daily_stat"],
            paper_mode=True,
        )
        await portfolio.sync_from_db()

        # Alternate between edge and no-edge cycles
        markets_with_edge = [
            _make_market(condition_id=f"e2e_mkt_{i}", yes_price=0.45, no_price=0.55)
            for i in range(5)
        ]
        estimate_edge = _make_estimate(probability=0.70, confidence=0.82)
        estimate_no_edge = _make_estimate(probability=0.42, confidence=0.82)

        pipeline, mock_poly, mock_ws, _, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate_edge
        )

        total_cycles = 10
        positions_opened = 0

        for cycle in range(total_cycles):
            if cycle < 5:
                # First 5 cycles: fresh markets with edge
                market_idx = cycle
                mock_poly.get_active_markets.return_value = [markets_with_edge[market_idx]]
                pipeline._estimator.estimate.return_value = estimate_edge
            else:
                # Cycles 6-10: close positions (spike prices to take-profit)
                mock_poly.get_active_markets.return_value = []
                # Spike all open positions to take-profit territory
                mock_ws.get_price.return_value = 0.97

            await pipeline.run_cycle()

            # State consistency checks
            assert portfolio.bankroll >= 0, "Bankroll should never go negative"
            assert portfolio.open_positions_count >= 0
            assert portfolio.peak_bankroll >= portfolio.bankroll

        # After 10 cycles, all positions should be closed (take-profit at 0.97)
        assert portfolio.open_positions_count == 0

        # Total trades recorded in DB
        trades = await repos["trade"].get_recent_trades(50)
        assert len(trades) >= 1  # At least some trades happened

        # Verify summary is consistent
        summary = portfolio.summary()
        assert "bankroll" in summary
        assert "open_positions" in summary
        assert "total_trades" in summary
        assert summary["paper_mode"] is True

    async def test_bankroll_consistency_across_cycles(self, db, settings, repos):
        """Portfolio bankroll math stays consistent across open/close cycles."""
        portfolio = Portfolio(
            initial_bankroll=50.0,
            trade_repo=repos["trade"],
            daily_stat_repo=repos["daily_stat"],
            paper_mode=True,
        )
        await portfolio.sync_from_db()

        estimate = _make_estimate(probability=0.70, confidence=0.82)
        pipeline, mock_poly, mock_ws, _, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )

        # Open 2 positions on distinct markets
        for i in range(2):
            mock_poly.get_active_markets.return_value = [
                _make_market(condition_id=f"bank_mkt_{i}", yes_price=0.45, no_price=0.55)
            ]
            await pipeline.run_cycle()

        assert portfolio.open_positions_count == 2
        bankroll_with_open = portfolio.bankroll
        total_invested = sum(p.size for p in portfolio.open_positions)
        # Cash + invested = starting bankroll
        assert abs(bankroll_with_open + total_invested - 50.0) < 0.01

        # Close all via near-resolution (price → 0.97, profitable exit)
        mock_poly.get_active_markets.return_value = []
        mock_ws.get_price.return_value = 0.97

        await pipeline.run_cycle()

        # All positions closed
        assert portfolio.open_positions_count == 0
        # Bankroll should exceed original (profitable close)
        assert portfolio.bankroll > 50.0

    async def test_calibration_recorded_for_trades(self, db, settings, repos, portfolio):
        """Calibration predictions are recorded in DB for each executed trade."""
        estimate = _make_estimate(probability=0.70, confidence=0.82)
        pipeline, mock_poly, _, _, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )
        mock_poly.get_active_markets.return_value = [_make_market(yes_price=0.45, no_price=0.55)]

        await pipeline.run_cycle()

        # Prediction recorded
        preds = await repos["prediction"].get_unresolved_predictions()
        assert len(preds) == 1
        pred = preds[0]
        assert pred.market_id == "market_001"
        assert abs(pred.estimated_prob - 0.70) < 0.01
        assert abs(pred.confidence - 0.82) < 0.01

    async def test_event_bus_receives_trade_event(self, db, settings, repos, portfolio):
        """EventBus publishes trade_executed event after a successful paper trade."""
        estimate = _make_estimate(probability=0.70, confidence=0.82)
        event_bus = EventBus()
        received_events = []
        captured_types = []

        async def on_trade(**kwargs):
            captured_types.append("trade_executed")
            received_events.append(("trade_executed", kwargs))

        async def on_cycle(**kwargs):
            captured_types.append("cycle_complete")

        event_bus.subscribe("trade_executed", on_trade)
        event_bus.subscribe("cycle_complete", on_cycle)

        pipeline, mock_poly, _, _, _ = _make_pipeline(
            settings, portfolio, repos, estimate=estimate
        )
        # Replace the event bus with our instrumented one
        pipeline._event_bus = event_bus

        mock_poly.get_active_markets.return_value = [_make_market(yes_price=0.45, no_price=0.55)]
        await pipeline.run_cycle()

        assert "trade_executed" in captured_types
        assert "cycle_complete" in captured_types

        trade_event = next(e[1] for e in received_events if e[0] == "trade_executed")
        assert trade_event["market_id"] == "market_001"
        assert trade_event["paper_mode"] is True
