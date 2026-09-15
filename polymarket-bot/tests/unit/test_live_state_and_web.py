import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config.settings import Settings
from storage.database import close_db, init_db
from storage.repository import DailyStatRepository, MarketRepository, TradeRepository
from trading.executor import OrderExecutor
from trading.portfolio import Portfolio, Position
from trading.risk_manager import TradeProposal
from web.server import create_app


def _make_proposal(**overrides) -> TradeProposal:
    defaults = {
        "market_id": "mkt_live_001",
        "token_id": "tok_live_001",
        "question": "THRESH BTC >$100,000 | Test market",
        "direction": "BUY_YES",
        "price": 0.55,
        "size": 12.0,
        "edge": 0.12,
        "estimated_prob": 0.67,
        "confidence": 0.82,
        "kelly_fraction": 0.09,
        "reasoning": "BS test reasoning | end=2026-04-24T18:00:00+00:00",
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


class TestLiveState:
    async def test_gtc_entry_returns_pending_without_opening_position(self, db, trade_repo):
        mock_poly = MagicMock()
        mock_poly.get_balance = AsyncMock(return_value=250.0)
        mock_poly.get_tick_size = AsyncMock(return_value=0.01)
        mock_poly.place_limit_order = AsyncMock(return_value={"orderID": "gtc-order-1"})
        mock_poly.get_order = AsyncMock(return_value={"status": "OPEN"})

        executor = OrderExecutor(
            polymarket=mock_poly,
            trade_repo=trade_repo,
            paper_mode=False,
        )

        result = await executor.execute(_make_proposal())
        assert result.success is True
        assert result.pending is True
        assert result.fill_price == 0.0
        assert result.fill_size == 0.0

        trades = await trade_repo.get_open_trades(paper_mode=False)
        assert len(trades) == 1
        assert trades[0].status == "open"
        assert trades[0].side == "BUY"

        await executor.cancel_pending_gtc_tasks()

    async def test_close_live_uses_immediate_fill_and_does_not_create_exit_trade(self, db, trade_repo):
        mock_poly = MagicMock()
        mock_poly.get_condition_payouts = AsyncMock(return_value={"resolved": False})
        mock_poly.get_midpoint = AsyncMock(return_value=0.61)
        mock_poly.get_tick_size = AsyncMock(return_value=0.01)
        mock_poly.get_orderbook = AsyncMock(return_value={
            "bids": [{"price": "0.60", "size": "50"}],
            "asks": [],
        })
        mock_poly.place_fok_order = AsyncMock(return_value={
            "orderID": "sell-order-1",
            "status": "MATCHED",
            "price": "0.60",
            "size_matched": "20",
        })

        executor = OrderExecutor(
            polymarket=mock_poly,
            trade_repo=trade_repo,
            paper_mode=False,
        )
        pos = Position(
            trade_id=101,
            market_id="mkt_close",
            token_id="tok_close",
            question="THRESH ETH >$2,300 | Close test",
            direction="BUY_YES",
            entry_price=0.50,
            size=10.0,
        )

        result = await executor.close_live(pos, reason="test_close")
        assert result.success is True
        assert result.pending is False
        assert result.fill_price == pytest.approx(0.60)
        assert result.fill_size == pytest.approx(12.0)

        trades = await trade_repo.get_recent_trades(10)
        assert trades == []

    async def test_close_live_redeems_resolved_market_from_receipt_payout(self, db, trade_repo):
        mock_poly = MagicMock()
        mock_poly.get_condition_payouts = AsyncMock(return_value={
            "resolved": True,
            "denominator": 1,
            "numerators": [0, 1],
        })
        mock_poly.redeem_resolved_position = AsyncMock(return_value={
            "success": True,
            "tx_hash": "0xredeem",
            "payout": 15.0,
            "shares": 30.0,
        })
        mock_poly.get_midpoint = AsyncMock()

        executor = OrderExecutor(
            polymarket=mock_poly,
            trade_repo=trade_repo,
            paper_mode=False,
        )
        pos = Position(
            trade_id=102,
            market_id="mkt_resolved",
            token_id="tok_resolved",
            question="WEATHER Test",
            direction="BUY_NO",
            entry_price=0.50,
            size=10.0,
        )

        result = await executor.close_live(pos, reason="resolution")
        assert result.success is True
        assert result.order_id == "0xredeem"
        assert result.fill_size == pytest.approx(15.0)
        assert result.fill_price == pytest.approx(0.75)
        mock_poly.get_midpoint.assert_not_called()

    async def test_sync_from_db_restores_only_filled_entry_trades(self, db, trade_repo):
        daily_repo = DailyStatRepository()
        await trade_repo.create_trade(
            market_id="mkt_closed",
            token_id="tok_closed",
            question="Closed winner",
            side="BUY",
            direction="BUY_YES",
            price=0.50,
            size=10.0,
            edge=0.1,
            kelly_fraction=0.05,
            estimated_prob=0.61,
            market_price_at_entry=0.50,
            confidence=0.8,
            status="closed",
            pnl=25.0,
            paper_mode=False,
            order_id="ord-closed",
            reasoning="winner",
        )
        await trade_repo.create_trade(
            market_id="mkt_open",
            token_id="tok_open",
            question="Pending GTC",
            side="BUY",
            direction="BUY_YES",
            price=0.51,
            size=8.0,
            edge=0.1,
            kelly_fraction=0.05,
            estimated_prob=0.61,
            market_price_at_entry=0.51,
            confidence=0.8,
            status="open",
            paper_mode=False,
            order_id="ord-open",
            reasoning="pending",
        )
        await trade_repo.create_trade(
            market_id="mkt_filled",
            token_id="tok_filled",
            question="Held position",
            side="BUY",
            direction="BUY_YES",
            price=0.48,
            size=9.0,
            edge=0.1,
            kelly_fraction=0.05,
            estimated_prob=0.61,
            market_price_at_entry=0.48,
            confidence=0.8,
            status="filled",
            paper_mode=False,
            order_id="ord-filled",
            reasoning="held",
        )
        await trade_repo.create_trade(
            market_id="mkt_sell",
            token_id="tok_sell",
            question="Exit row should not restore",
            side="SELL",
            direction="SELL_YES",
            price=0.75,
            size=9.0,
            edge=0.0,
            kelly_fraction=0.0,
            estimated_prob=0.0,
            market_price_at_entry=0.48,
            confidence=0.0,
            status="filled",
            paper_mode=False,
            order_id="ord-sell",
            reasoning="exit",
        )
        await daily_repo.upsert_today(
            starting_bankroll=100.0,
            ending_bankroll=125.0,
            trades_count=0,
            wins=0,
            losses=0,
            daily_pnl=25.0,
            peak_bankroll=125.0,
        )

        portfolio = Portfolio(
            initial_bankroll=100.0,
            trade_repo=trade_repo,
            daily_stat_repo=daily_repo,
            paper_mode=False,
        )
        await portfolio.sync_from_db()

        assert portfolio.open_positions_count == 1
        assert portfolio.open_positions[0].market_id == "mkt_filled"
        assert portfolio.open_positions[0].current_price == pytest.approx(0.48)
        assert portfolio.bankroll == pytest.approx(116.0)
        summary = portfolio.summary()
        assert summary["total_trades"] == 1
        assert summary["wins"] == 1
        assert summary["losses"] == 0
        assert summary["daily_pnl"] == pytest.approx(25.0)


class TestWebDashboard:
    async def _client(self, app):
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    async def test_positions_include_expected_close(self, db, trade_repo):
        market_repo = MarketRepository()
        await market_repo.upsert_market(
            condition_id="mkt_pos_1",
            question="Will BTC be above $100k?",
            end_date="2026-04-24T18:00:00Z",
            volume=10000.0,
            yes_token_id="tok_pos_1",
            no_token_id="tok_pos_1_no",
        )
        trade = await trade_repo.create_trade(
            market_id="mkt_pos_1",
            token_id="tok_pos_1",
            question="THRESH BTC >$100,000 | Position test",
            side="BUY",
            direction="BUY_YES",
            price=0.55,
            size=12.0,
            edge=0.12,
            kelly_fraction=0.09,
            estimated_prob=0.67,
            market_price_at_entry=0.55,
            confidence=0.82,
            status="filled",
            paper_mode=True,
            order_id="paper_pos_1",
            reasoning="BS | end=2026-04-24T18:00:00+00:00",
        )

        portfolio = Portfolio(initial_bankroll=100.0, trade_repo=trade_repo, paper_mode=True)
        portfolio.open_position(
            trade_id=trade.id,
            market_id="mkt_pos_1",
            token_id="tok_pos_1",
            question=trade.question,
            direction=trade.direction,
            price=trade.price,
            size=trade.size,
        )

        app = create_app(portfolio=portfolio, settings=Settings())
        async with await self._client(app) as client:
            resp = await client.get("/api/positions")
        assert resp.status_code == 200
        assert "Expected Close" in resp.text
        assert "THRESH" in resp.text
        assert "04/24" in resp.text

    async def test_trades_filters_by_month_and_category(self, db, trade_repo):
        await trade_repo.create_trade(
            market_id="weather_apr",
            token_id="tok_w_apr",
            question="WEATHER New York | April trade",
            side="BUY",
            direction="BUY_YES",
            price=0.44,
            size=5.0,
            edge=0.1,
            kelly_fraction=0.05,
            estimated_prob=0.6,
            market_price_at_entry=0.44,
            confidence=0.8,
            status="closed",
            pnl=2.0,
            paper_mode=True,
            order_id="weather_apr",
            reasoning="target=2026-04-24",
            created_at=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
            closed_at=datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc),
        )
        await trade_repo.create_trade(
            market_id="thresh_may",
            token_id="tok_t_may",
            question="THRESH BTC >$100,000 | May trade",
            side="BUY",
            direction="BUY_YES",
            price=0.54,
            size=6.0,
            edge=0.1,
            kelly_fraction=0.05,
            estimated_prob=0.6,
            market_price_at_entry=0.54,
            confidence=0.8,
            status="closed",
            pnl=1.0,
            paper_mode=True,
            order_id="thresh_may",
            reasoning="end=2026-05-02T18:00:00+00:00",
            created_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            closed_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        )

        app = create_app(portfolio=Portfolio(initial_bankroll=100.0, paper_mode=True), settings=Settings())
        async with await self._client(app) as client:
            resp = await client.get("/api/trades", params={
                "month": "2026-04",
                "category": "WEATHER",
                "status": "closed",
            })
        assert resp.status_code == 200
        assert "April trade" in resp.text
        assert "May trade" not in resp.text
        assert "WEATHER" in resp.text

    async def test_equity_curve_uses_closed_trade_history(self, db, trade_repo):
        portfolio = Portfolio(
            initial_bankroll=100.0,
            trade_repo=trade_repo,
            paper_mode=True,
        )

        trade_win = await trade_repo.create_trade(
            market_id="mkt_win",
            token_id="tok_win",
            question="Win trade",
            side="BUY",
            direction="BUY_YES",
            price=0.50,
            size=10.0,
            edge=0.1,
            kelly_fraction=0.05,
            estimated_prob=0.6,
            market_price_at_entry=0.50,
            confidence=0.8,
            status="filled",
            paper_mode=True,
            order_id="paper_win",
            reasoning="win",
            created_at=datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc),
        )
        portfolio.open_position(
            trade_id=trade_win.id,
            market_id="mkt_win",
            token_id="tok_win",
            question="Win trade",
            direction="BUY_YES",
            price=0.50,
            size=10.0,
        )
        await portfolio.close_position("mkt_win", 0.75)  # +5

        trade_loss = await trade_repo.create_trade(
            market_id="mkt_loss",
            token_id="tok_loss",
            question="Loss trade",
            side="BUY",
            direction="BUY_YES",
            price=0.50,
            size=10.0,
            edge=0.1,
            kelly_fraction=0.05,
            estimated_prob=0.6,
            market_price_at_entry=0.50,
            confidence=0.8,
            status="filled",
            paper_mode=True,
            order_id="paper_loss",
            reasoning="loss",
            created_at=datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc),
        )
        portfolio.open_position(
            trade_id=trade_loss.id,
            market_id="mkt_loss",
            token_id="tok_loss",
            question="Loss trade",
            direction="BUY_YES",
            price=0.50,
            size=10.0,
        )
        await portfolio.close_position("mkt_loss", 0.40)  # -2

        app = create_app(portfolio=portfolio, settings=Settings())
        async with await self._client(app) as client:
            resp = await client.get("/api/equity")
        assert resp.status_code == 200
        assert "equity-data" in resp.text
        assert "105.0" in resp.text
        assert "103.0" in resp.text
