import pytest

from trading.risk_manager import RiskManager, TradeProposal


def make_proposal(**overrides) -> TradeProposal:
    defaults = {
        "market_id": "market_1",
        "token_id": "token_1",
        "question": "Will X happen?",
        "direction": "BUY_YES",
        "price": 0.50,
        "size": 2.0,
        "edge": 0.15,
        "estimated_prob": 0.65,
        "confidence": 0.80,
        "kelly_fraction": 0.10,
    }
    defaults.update(overrides)
    return TradeProposal(**defaults)


class TestRiskManager:
    def setup_method(self):
        self.rm = RiskManager(
            max_position_pct=0.20,
            max_open_positions_small=3,
            max_open_positions_large=5,
            daily_loss_limit=0.30,
            drawdown_pause=0.50,
            min_edge=0.10,
            min_confidence=0.75,
        )

    @pytest.mark.asyncio
    async def test_approve_valid_trade(self):
        result = await self.rm.approve(
            proposal=make_proposal(),
            bankroll=100.0,
            peak_bankroll=100.0,
            daily_pnl=0.0,
            open_positions_count=0,
            held_market_ids=set(),
            market_volume=10000,
        )
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_reject_low_edge(self):
        result = await self.rm.approve(
            proposal=make_proposal(edge=0.05),
            bankroll=100.0,
            peak_bankroll=100.0,
            daily_pnl=0.0,
            open_positions_count=0,
            held_market_ids=set(),
        )
        assert result.approved is False
        assert "Edge" in result.reason

    @pytest.mark.asyncio
    async def test_reject_low_confidence(self):
        result = await self.rm.approve(
            proposal=make_proposal(confidence=0.60),
            bankroll=100.0,
            peak_bankroll=100.0,
            daily_pnl=0.0,
            open_positions_count=0,
            held_market_ids=set(),
        )
        assert result.approved is False
        assert "Confidence" in result.reason

    @pytest.mark.asyncio
    async def test_reject_daily_loss_limit(self):
        result = await self.rm.approve(
            proposal=make_proposal(),
            bankroll=100.0,
            peak_bankroll=100.0,
            daily_pnl=-35.0,  # -35% > -30% limit
            open_positions_count=0,
            held_market_ids=set(),
        )
        assert result.approved is False
        assert "Daily loss" in result.reason

    @pytest.mark.asyncio
    async def test_reject_drawdown(self):
        result = await self.rm.approve(
            proposal=make_proposal(),
            bankroll=40.0,  # 60% drawdown from peak of 100
            peak_bankroll=100.0,
            daily_pnl=0.0,
            open_positions_count=0,
            held_market_ids=set(),
        )
        assert result.approved is False
        assert "Drawdown" in result.reason
        assert self.rm.is_paused

    @pytest.mark.asyncio
    async def test_reject_max_positions_small_bankroll(self):
        result = await self.rm.approve(
            proposal=make_proposal(),
            bankroll=30.0,  # < $50, so max 3 positions
            peak_bankroll=30.0,
            daily_pnl=0.0,
            open_positions_count=3,  # already at max
            held_market_ids=set(),
        )
        assert result.approved is False
        assert "positions" in result.reason

    @pytest.mark.asyncio
    async def test_allow_more_positions_large_bankroll(self):
        result = await self.rm.approve(
            proposal=make_proposal(),
            bankroll=100.0,  # >= $50, so max 5 positions
            peak_bankroll=100.0,
            daily_pnl=0.0,
            open_positions_count=3,  # < 5, allowed
            held_market_ids=set(),
            market_volume=10000,
        )
        assert result.approved is True

    @pytest.mark.asyncio
    async def test_reject_duplicate_market(self):
        result = await self.rm.approve(
            proposal=make_proposal(market_id="market_1"),
            bankroll=100.0,
            peak_bankroll=100.0,
            daily_pnl=0.0,
            open_positions_count=0,
            held_market_ids={"market_1"},
        )
        assert result.approved is False
        assert "Already holding" in result.reason

    @pytest.mark.asyncio
    async def test_reject_position_too_large(self):
        result = await self.rm.approve(
            proposal=make_proposal(size=25.0),  # 25% > 20% max
            bankroll=100.0,
            peak_bankroll=100.0,
            daily_pnl=0.0,
            open_positions_count=0,
            held_market_ids=set(),
        )
        assert result.approved is False

    @pytest.mark.asyncio
    async def test_reject_insufficient_balance(self):
        result = await self.rm.approve(
            proposal=make_proposal(size=15.0),
            bankroll=10.0,
            peak_bankroll=10.0,
            daily_pnl=0.0,
            open_positions_count=0,
            held_market_ids=set(),
        )
        assert result.approved is False

    @pytest.mark.asyncio
    async def test_reject_low_liquidity(self):
        result = await self.rm.approve(
            proposal=make_proposal(),
            bankroll=100.0,
            peak_bankroll=100.0,
            daily_pnl=0.0,
            open_positions_count=0,
            held_market_ids=set(),
            market_volume=1000,  # < 5000 min
        )
        assert result.approved is False

    def test_pause_and_resume(self):
        self.rm.pause(1.0)
        assert self.rm.is_paused is True
        self.rm.resume()
        assert self.rm.is_paused is False
