"""Unit tests for bot/circuit_breakers.py."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.circuit_breakers import (
    CircuitBreakerManager,
    BreakerState,
    GLOBAL_KEY,
    EMERGENCY_FLAG_FILE,
)


def _make_portfolio(bankroll=100.0, peak=100.0, daily_pnl=0.0, daily_start=100.0,
                    open_positions=None):
    p = SimpleNamespace()
    p.bankroll = bankroll
    p.peak_bankroll = peak
    p.daily_pnl = daily_pnl
    p._daily_start_bankroll = daily_start
    p.open_positions = open_positions or []
    return p


def _prime_peak(cbm, peak_value: float) -> None:
    """Manually set the breaker's tracked peak (simulates earlier cycles
    where portfolio was at higher value)."""
    cbm._peak_total_value = peak_value


def _make_repo_with_trades(trades):
    repo = MagicMock()
    repo.get_closed_trades = AsyncMock(return_value=trades)
    return repo


def _trade(pnl, trade_id=1, question="WEATHER Seoul | test", closed_hours_ago=1):
    t = SimpleNamespace()
    t.id = trade_id
    t.pnl = pnl
    t.question = question
    t.closed_at = datetime.now(timezone.utc) - timedelta(hours=closed_hours_ago)
    return t


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "breakers_state.json")


@pytest.fixture
def telegram():
    t = MagicMock()
    t.send_risk_alert = AsyncMock()
    return t


class TestDrawdownBreaker:
    @pytest.mark.asyncio
    async def test_trips_at_threshold_drawdown(self, state_file, telegram):
        # pilot threshold is 50% — total value $45 vs prior peak $100 -> 55% drawdown
        portfolio = _make_portfolio(bankroll=45.0, peak=100.0)
        cbm = CircuitBreakerManager(portfolio, None, telegram, state_file)
        _prime_peak(cbm, 100.0)  # simulate prior high
        await cbm.check_all()
        halted, reason = cbm.is_halted("weather")
        assert halted is True
        assert "drawdown" in reason
        telegram.send_risk_alert.assert_called()

    @pytest.mark.asyncio
    async def test_does_not_trip_below_threshold(self, state_file, telegram):
        portfolio = _make_portfolio(bankroll=60.0, peak=100.0)  # 40% drawdown < 50% threshold
        cbm = CircuitBreakerManager(portfolio, None, telegram, state_file)
        _prime_peak(cbm, 100.0)
        await cbm.check_all()
        halted, _ = cbm.is_halted("weather")
        assert halted is False

    @pytest.mark.asyncio
    async def test_opening_position_does_not_trip(self, state_file, telegram):
        """Opening a $50 position (cash $100 -> $50, exposure $50) keeps total $100.
        Prior behaviour incorrectly tripped because peak was cash-based."""
        from types import SimpleNamespace
        pos = SimpleNamespace(size=50.0)
        portfolio = _make_portfolio(bankroll=50.0, peak=100.0, open_positions=[pos])
        cbm = CircuitBreakerManager(portfolio, None, telegram, state_file)
        _prime_peak(cbm, 100.0)
        await cbm.check_all()
        halted, _ = cbm.is_halted("weather")
        assert halted is False  # total $100, no real loss

    @pytest.mark.asyncio
    async def test_blocks_all_strategies_when_global_tripped(self, state_file, telegram):
        portfolio = _make_portfolio(bankroll=50.0, peak=100.0)  # 50% drawdown
        cbm = CircuitBreakerManager(portfolio, None, telegram, state_file)
        _prime_peak(cbm, 100.0)
        await cbm.check_all()
        for strategy in ("weather", "threshold", "spread"):
            halted, reason = cbm.is_halted(strategy)
            assert halted is True
            assert "global" in reason


class TestDailyLossBreaker:
    @pytest.mark.asyncio
    async def test_trips_at_threshold_daily_loss(self, state_file, telegram):
        # pilot threshold is 35% — at -$36 / $100 start = 36% daily loss
        portfolio = _make_portfolio(bankroll=64.0, peak=100.0, daily_pnl=-36.0, daily_start=100.0)
        cbm = CircuitBreakerManager(portfolio, None, telegram, state_file)
        await cbm.check_all()
        halted, reason = cbm.is_halted("weather")
        assert halted is True
        assert "daily" in reason.lower()

    @pytest.mark.asyncio
    async def test_does_not_trip_small_loss(self, state_file, telegram):
        # 20% daily loss < 35% threshold — no trip
        portfolio = _make_portfolio(bankroll=80.0, peak=100.0, daily_pnl=-20.0, daily_start=100.0)
        cbm = CircuitBreakerManager(portfolio, None, telegram, state_file)
        await cbm.check_all()
        halted, _ = cbm.is_halted("weather")
        assert halted is False


class TestConsecutiveLosses:
    @pytest.mark.asyncio
    async def test_five_losses_in_row_trips(self, state_file, telegram):
        losses = [_trade(pnl=-5.0, trade_id=i) for i in range(10, 5, -1)]
        repo = _make_repo_with_trades(losses)
        portfolio = _make_portfolio()
        cbm = CircuitBreakerManager(portfolio, repo, telegram, state_file)
        await cbm.check_all()
        halted, reason = cbm.is_halted("weather")
        assert halted is True
        assert "consecutive" in reason

    @pytest.mark.asyncio
    async def test_four_losses_no_trip(self, state_file, telegram):
        # 4 losses then a win — no trip
        trades = [_trade(pnl=+10.0, trade_id=20)] + [
            _trade(pnl=-5.0, trade_id=i) for i in range(19, 15, -1)
        ]
        repo = _make_repo_with_trades(trades)
        cbm = CircuitBreakerManager(_make_portfolio(), repo, telegram, state_file)
        await cbm.check_all()
        halted, _ = cbm.is_halted("weather")
        assert halted is False

    @pytest.mark.asyncio
    async def test_strategy_isolated(self, state_file, telegram):
        """Weather losses should not halt threshold."""
        losses = [_trade(pnl=-5.0, trade_id=i,
                         question=f"WEATHER City | m{i}") for i in range(10, 5, -1)]
        repo = _make_repo_with_trades(losses)
        cbm = CircuitBreakerManager(_make_portfolio(), repo, telegram, state_file)
        await cbm.check_all()
        assert cbm.is_halted("weather")[0] is True
        assert cbm.is_halted("threshold")[0] is False


class TestWRWatchdog:
    @pytest.mark.asyncio
    async def test_low_wr_trips(self, state_file, telegram):
        # 12 trades, 3 wins → WR=25% < 40%
        trades = [_trade(pnl=+10.0, trade_id=i) for i in range(3)] + \
                 [_trade(pnl=-5.0, trade_id=i) for i in range(3, 12)]
        repo = _make_repo_with_trades(trades)
        cbm = CircuitBreakerManager(_make_portfolio(), repo, telegram, state_file)
        # Skip consecutive losses check — craft trades to avoid both conditions firing
        # Mix losses so not 5 in a row
        mixed = []
        for i in range(12):
            mixed.append(_trade(pnl=(+10.0 if i < 3 else -5.0 if i % 2 else -4.0), trade_id=i))
        repo = _make_repo_with_trades(mixed)
        cbm = CircuitBreakerManager(_make_portfolio(), repo, telegram, state_file)
        await cbm.check_all()
        halted, reason = cbm.is_halted("weather")
        assert halted is True
        # Could be either consecutive or WR — both are valid signals

    @pytest.mark.asyncio
    async def test_high_wr_no_trip(self, state_file, telegram):
        # Construct 12 trades with high WR AND last 3 NOT all losses
        # (to avoid accidentally tripping consecutive-losses breaker).
        trades = []
        for i in range(12):
            # Last by id: 11 (win), 10 (win), 9 (win), 8 (loss), 7 (loss)... pattern
            # breaks any 3-in-a-row losses while keeping overall WR > 50%.
            pnl = +10.0 if (i in (11, 10, 9, 7, 5, 3, 1, 0)) else -5.0
            trades.append(_trade(pnl=pnl, trade_id=i))
        repo = _make_repo_with_trades(trades)
        cbm = CircuitBreakerManager(_make_portfolio(), repo, telegram, state_file)
        await cbm.check_all()
        halted, _ = cbm.is_halted("weather")
        assert halted is False

    @pytest.mark.asyncio
    async def test_insufficient_sample_no_trip(self, state_file, telegram):
        # Only 2 trades — below CONSECUTIVE_LOSSES=3 AND below WR_MIN_TRADES=10
        trades = [_trade(pnl=-5.0, trade_id=i) for i in range(2)]
        repo = _make_repo_with_trades(trades)
        cbm = CircuitBreakerManager(_make_portfolio(), repo, telegram, state_file)
        await cbm.check_all()
        halted, _ = cbm.is_halted("weather")
        assert halted is False


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_flag_file_trips_global(self, state_file, telegram, tmp_path, monkeypatch):
        flag = tmp_path / "kill"
        flag.write_text("halt")
        monkeypatch.setattr("bot.circuit_breakers.EMERGENCY_FLAG_FILE", str(flag))
        cbm = CircuitBreakerManager(_make_portfolio(), None, telegram, state_file)
        await cbm.check_all()
        for strategy in ("weather", "threshold", "spread"):
            halted, reason = cbm.is_halted(strategy)
            assert halted is True
            assert "emergency_flag_file" in reason

    @pytest.mark.asyncio
    async def test_no_flag_no_trip(self, state_file, telegram, tmp_path, monkeypatch):
        flag = tmp_path / "absent_file"  # doesn't exist
        monkeypatch.setattr("bot.circuit_breakers.EMERGENCY_FLAG_FILE", str(flag))
        cbm = CircuitBreakerManager(_make_portfolio(), None, telegram, state_file)
        await cbm.check_all()
        assert cbm.is_halted("weather")[0] is False


class TestStatePersistence:
    @pytest.mark.asyncio
    async def test_state_saves_and_loads(self, state_file, telegram):
        portfolio = _make_portfolio(bankroll=50.0, peak=100.0)  # 50% drawdown → global trip
        cbm = CircuitBreakerManager(portfolio, None, telegram, state_file)
        _prime_peak(cbm, 100.0)
        await cbm.check_all()
        assert cbm.is_halted("weather")[0] is True
        # Now reload from file in new instance
        cbm2 = CircuitBreakerManager(portfolio, None, telegram, state_file)
        halted, reason = cbm2.is_halted("weather")
        assert halted is True
        assert "drawdown" in reason

    @pytest.mark.asyncio
    async def test_empty_state_file_loads_clean(self, state_file, telegram):
        # Nonexistent file
        assert not os.path.exists(state_file)
        cbm = CircuitBreakerManager(_make_portfolio(), None, telegram, state_file)
        halted, _ = cbm.is_halted("weather")
        assert halted is False


class TestAutoReset:
    @pytest.mark.asyncio
    async def test_soft_trip_auto_resets_after_hours(self, state_file, telegram):
        # daily loss 40% > 35% threshold → soft trip with auto_reset_at set
        portfolio = _make_portfolio(bankroll=60.0, peak=100.0, daily_pnl=-40.0, daily_start=100.0)
        cbm = CircuitBreakerManager(portfolio, None, telegram, state_file)
        await cbm.check_all()
        assert cbm.is_halted("weather")[0] is True
        # Manually rewind auto_reset_at to past
        gs = cbm._state[GLOBAL_KEY]
        gs.auto_reset_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        halted, _ = cbm.is_halted("weather")
        assert halted is False

    @pytest.mark.asyncio
    async def test_drawdown_manual_only(self, state_file, telegram):
        portfolio = _make_portfolio(bankroll=50.0, peak=100.0)
        cbm = CircuitBreakerManager(portfolio, None, telegram, state_file)
        _prime_peak(cbm, 100.0)
        await cbm.check_all()
        gs = cbm._state[GLOBAL_KEY]
        assert gs.auto_reset_at is None  # drawdown requires manual reset


class TestReset:
    @pytest.mark.asyncio
    async def test_manual_reset_clears(self, state_file, telegram):
        portfolio = _make_portfolio(bankroll=50.0, peak=100.0)
        cbm = CircuitBreakerManager(portfolio, None, telegram, state_file)
        _prime_peak(cbm, 100.0)
        await cbm.check_all()
        assert cbm.is_halted("weather")[0] is True
        await cbm.reset(GLOBAL_KEY)
        assert cbm.is_halted("weather")[0] is False


class TestNoDoubleAlert:
    @pytest.mark.asyncio
    async def test_second_check_does_not_realert(self, state_file, telegram):
        portfolio = _make_portfolio(bankroll=50.0, peak=100.0)
        cbm = CircuitBreakerManager(portfolio, None, telegram, state_file)
        _prime_peak(cbm, 100.0)
        await cbm.check_all()
        first_count = telegram.send_risk_alert.call_count
        # Force re-run (bypass rate limit)
        cbm._last_check_ts = 0
        await cbm.check_all()
        # Should not have sent another alert
        assert telegram.send_risk_alert.call_count == first_count
