"""Unit tests for OrderExecutor: paper mode execution and order creation."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from storage.database import close_db, init_db
from storage.repository import TradeRepository
from trading.executor import ExecutionResult, OrderExecutor
from trading.risk_manager import TradeProposal


def _make_proposal(**overrides) -> TradeProposal:
    defaults = {
        "market_id": "mkt_001",
        "token_id": "tok_yes_001",
        "question": "Will X happen?",
        "direction": "BUY_YES",
        "price": 0.50,
        "size": 5.0,
        "edge": 0.15,
        "estimated_prob": 0.65,
        "confidence": 0.80,
        "kelly_fraction": 0.10,
        "reasoning": "Strong evidence supports YES outcome.",
    }
    defaults.update(overrides)
    return TradeProposal(**defaults)


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    await init_db(db_path)
    yield db_path
    await close_db()


@pytest.fixture
async def trade_repo(db):
    return TradeRepository()


class TestPaperExecution:
    async def test_paper_execute_success(self, db, trade_repo):
        """Paper execute returns success with correct fill price and size."""
        executor = OrderExecutor(
            polymarket=MagicMock(),
            trade_repo=trade_repo,
            paper_mode=True,
        )
        proposal = _make_proposal(price=0.55, size=8.0)
        result = await executor.execute(proposal)

        assert result.success is True
        assert result.fill_price == pytest.approx(0.55)
        assert result.fill_size == pytest.approx(8.0)
        assert result.paper_mode is True
        assert result.trade_id > 0

    async def test_paper_execute_creates_db_record(self, db, trade_repo):
        """Paper execute persists a filled trade to the DB."""
        executor = OrderExecutor(
            polymarket=MagicMock(),
            trade_repo=trade_repo,
            paper_mode=True,
        )
        proposal = _make_proposal(market_id="paper_mkt", price=0.45, size=6.5)
        result = await executor.execute(proposal)

        assert result.success is True
        trades = await trade_repo.get_open_trades(paper_mode=True)
        assert len(trades) == 1
        t = trades[0]
        assert t.market_id == "paper_mkt"
        assert t.price == pytest.approx(0.45)
        assert t.size == pytest.approx(6.5)
        assert t.status == "filled"
        assert t.paper_mode is True

    async def test_paper_execute_direction_recorded(self, db, trade_repo):
        """Direction (BUY_YES vs BUY_NO) is stored correctly."""
        executor = OrderExecutor(
            polymarket=MagicMock(),
            trade_repo=trade_repo,
            paper_mode=True,
        )
        for direction in ("BUY_YES", "BUY_NO"):
            proposal = _make_proposal(
                market_id=f"mkt_{direction}",
                direction=direction,
            )
            result = await executor.execute(proposal)
            assert result.success is True

        trades = await trade_repo.get_open_trades(paper_mode=True)
        directions = {t.market_id: t.direction for t in trades}
        assert directions["mkt_BUY_YES"] == "BUY_YES"
        assert directions["mkt_BUY_NO"] == "BUY_NO"

    async def test_paper_execute_reasoning_stored(self, db, trade_repo):
        """Reasoning text is stored in the trade record."""
        executor = OrderExecutor(
            polymarket=MagicMock(),
            trade_repo=trade_repo,
            paper_mode=True,
        )
        proposal = _make_proposal(reasoning="Positive earnings beat expectations.")
        await executor.execute(proposal)

        trades = await trade_repo.get_recent_trades(1)
        assert "earnings" in trades[0].reasoning.lower()

    async def test_paper_execute_edge_and_confidence_stored(self, db, trade_repo):
        """Edge and confidence values are persisted."""
        executor = OrderExecutor(
            polymarket=MagicMock(),
            trade_repo=trade_repo,
            paper_mode=True,
        )
        proposal = _make_proposal(edge=0.18, confidence=0.85, estimated_prob=0.68)
        await executor.execute(proposal)

        trades = await trade_repo.get_recent_trades(1)
        t = trades[0]
        assert t.edge == pytest.approx(0.18)
        assert t.confidence == pytest.approx(0.85)
        assert t.estimated_prob == pytest.approx(0.68)

    async def test_paper_execute_order_id_set(self, db, trade_repo):
        """Paper order ID follows expected format (starts with 'paper_')."""
        executor = OrderExecutor(
            polymarket=MagicMock(),
            trade_repo=trade_repo,
            paper_mode=True,
        )
        result = await executor.execute(_make_proposal())
        assert result.order_id.startswith("paper_")

    async def test_cancel_pending_gtc_tasks_noop_in_paper(self, db, trade_repo):
        """cancel_pending_gtc_tasks is safe to call with no pending tasks."""
        executor = OrderExecutor(
            polymarket=MagicMock(),
            trade_repo=trade_repo,
            paper_mode=True,
        )
        # Should not raise
        await executor.cancel_pending_gtc_tasks()

    async def test_multiple_paper_trades_independent(self, db, trade_repo):
        """Multiple paper trades are stored independently."""
        executor = OrderExecutor(
            polymarket=MagicMock(),
            trade_repo=trade_repo,
            paper_mode=True,
        )
        for i in range(3):
            proposal = _make_proposal(
                market_id=f"mkt_{i}",
                price=0.40 + i * 0.05,
                size=2.0 + i,
            )
            result = await executor.execute(proposal)
            assert result.success is True

        trades = await trade_repo.get_open_trades(paper_mode=True)
        assert len(trades) == 3
        prices = sorted(t.price for t in trades)
        assert prices == pytest.approx([0.40, 0.45, 0.50])

    async def test_gtc_timeout_cooldown_blocks_immediate_reentry(self, db, trade_repo):
        executor = OrderExecutor(
            polymarket=MagicMock(),
            trade_repo=trade_repo,
            paper_mode=False,
        )

        executor._cooldown_gtc_market({"market_id": "mkt_cooldown"}, "timeout")

        assert "mkt_cooldown" in executor.pending_market_ids
