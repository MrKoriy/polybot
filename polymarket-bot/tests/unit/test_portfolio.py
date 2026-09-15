import pytest

from trading.portfolio import Portfolio


class TestPortfolio:
    def setup_method(self):
        self.portfolio = Portfolio(initial_bankroll=100.0, paper_mode=True)

    def test_initial_state(self):
        assert self.portfolio.bankroll == 100.0
        assert self.portfolio.peak_bankroll == 100.0
        assert self.portfolio.open_positions_count == 0
        assert self.portfolio.win_rate == 0.0

    def test_open_position(self):
        self.portfolio.open_position(
            trade_id=1,
            market_id="m1",
            token_id="t1",
            question="Test?",
            direction="BUY_YES",
            price=0.50,
            size=10.0,
        )
        assert self.portfolio.bankroll == 90.0
        assert self.portfolio.open_positions_count == 1
        assert "m1" in self.portfolio.held_market_ids

    @pytest.mark.asyncio
    async def test_close_position_profit(self):
        self.portfolio.open_position(
            trade_id=1, market_id="m1", token_id="t1",
            question="Test?", direction="BUY_YES",
            price=0.50, size=10.0,
        )
        # Bought 20 shares at $0.50 each ($10 total)
        # Exit at $0.70 -> 20 * 0.70 = $14 -> PnL = +$4
        pnl = await self.portfolio.close_position("m1", 0.70)
        assert pnl == pytest.approx(4.0)
        assert self.portfolio.bankroll == pytest.approx(104.0)
        assert self.portfolio.open_positions_count == 0
        assert self.portfolio.win_rate == 1.0

    @pytest.mark.asyncio
    async def test_close_position_loss(self):
        self.portfolio.open_position(
            trade_id=1, market_id="m1", token_id="t1",
            question="Test?", direction="BUY_YES",
            price=0.50, size=10.0,
        )
        # Exit at $0.30 -> 20 * 0.30 = $6 -> PnL = -$4
        pnl = await self.portfolio.close_position("m1", 0.30)
        assert pnl == pytest.approx(-4.0)
        assert self.portfolio.bankroll == pytest.approx(96.0)
        assert self.portfolio.win_rate == 0.0

    @pytest.mark.asyncio
    async def test_peak_bankroll_tracking(self):
        self.portfolio.open_position(
            trade_id=1, market_id="m1", token_id="t1",
            question="Test?", direction="BUY_YES",
            price=0.50, size=10.0,
        )
        await self.portfolio.close_position("m1", 0.80)
        # Bankroll should be 90 + 20*0.8 = 90 + 16 = 106
        assert self.portfolio.bankroll == pytest.approx(106.0)
        assert self.portfolio.peak_bankroll == pytest.approx(106.0)

    def test_update_position_price(self):
        self.portfolio.open_position(
            trade_id=1, market_id="m1", token_id="t1",
            question="Test?", direction="BUY_YES",
            price=0.50, size=10.0,
        )
        self.portfolio.update_position_price("m1", 0.60)
        pos = self.portfolio.open_positions[0]
        assert pos.current_price == 0.60
        assert pos.unrealized_pnl == pytest.approx(2.0)  # 20 * 0.60 - 10 = 2

    def test_summary(self):
        summary = self.portfolio.summary()
        assert summary["bankroll"] == 100.0
        assert summary["open_positions"] == 0
        assert summary["paper_mode"] is True

    @pytest.mark.asyncio
    async def test_close_nonexistent_returns_zero(self):
        pnl = await self.portfolio.close_position("nonexistent", 0.50)
        assert pnl == 0.0
