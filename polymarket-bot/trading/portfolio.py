from dataclasses import dataclass, field
from datetime import datetime

import structlog

from storage.repository import DailyStatRepository, TradeRepository

log = structlog.get_logger()


@dataclass
class Position:
    trade_id: int
    market_id: str
    token_id: str
    question: str
    direction: str
    entry_price: float
    size: float
    current_price: float = 0.0
    opened_at: datetime | None = None

    @property
    def unrealized_pnl(self) -> float:
        if self.entry_price == 0:
            return 0.0
        # Shares bought = size / entry_price
        shares = self.size / self.entry_price
        return shares * self.current_price - self.size

    @property
    def pnl_pct(self) -> float:
        if self.size == 0:
            return 0.0
        return self.unrealized_pnl / self.size


class Portfolio:
    def __init__(
        self,
        initial_bankroll: float = 10.0,
        trade_repo: TradeRepository | None = None,
        daily_stat_repo: DailyStatRepository | None = None,
        paper_mode: bool = True,
    ) -> None:
        self._trade_repo = trade_repo
        self._daily_stat_repo = daily_stat_repo
        self._paper_mode = paper_mode
        self._initial_bankroll = initial_bankroll
        self._bankroll = initial_bankroll
        self._peak_bankroll = initial_bankroll
        self._daily_start_bankroll = initial_bankroll
        self._positions: dict[str, Position] = {}  # market_id -> Position
        self._total_trades = 0
        self._wins = 0
        self._losses = 0
        self._daily_pnl = 0.0
        self._daily_gross_loss = 0.0  # Sum of all losing trades today (absolute)
        self._daily_trades = 0
        self._daily_wins = 0
        self._daily_losses = 0
        self._last_day: str = datetime.utcnow().strftime("%Y-%m-%d")
        # Rolling window for adaptive Kelly (last 50 trades)
        self._recent_results: list[bool] = []  # True=win, False=loss
        self._rolling_window = 50

    @property
    def bankroll(self) -> float:
        return self._bankroll

    @property
    def peak_bankroll(self) -> float:
        return self._peak_bankroll

    @property
    def initial_bankroll(self) -> float:
        return self._initial_bankroll

    @property
    def daily_pnl(self) -> float:
        self._check_new_day()
        return self._daily_pnl

    @property
    def open_positions(self) -> list[Position]:
        return list(self._positions.values())

    @property
    def open_positions_count(self) -> int:
        return len(self._positions)

    @property
    def held_market_ids(self) -> set[str]:
        return set(self._positions.keys())

    @property
    def win_rate(self) -> float:
        if self._total_trades == 0:
            return 0.0
        return self._wins / self._total_trades

    @property
    def rolling_wr(self) -> float:
        """Win rate over last 50 trades — for adaptive Kelly."""
        if len(self._recent_results) < 10:
            return 0.55  # assume neutral until enough data
        return sum(self._recent_results) / len(self._recent_results)

    @property
    def kelly_multiplier(self) -> float:
        """Adaptive Kelly: reduce bet size when rolling WR drops.
        1.0 = full quarter-Kelly, 0.0 = don't trade.
        """
        wr = self.rolling_wr
        if wr >= 0.55:
            return 1.0  # full size
        elif wr >= 0.50:
            return (wr - 0.50) / 0.05  # linear 0→1 between 50-55%
        else:
            return 0.0  # WR below 50% = stop trading

    @property
    def daily_stop_hit(self) -> bool:
        """True if daily drawdown exceeds 20% of starting bankroll."""
        if self._daily_start_bankroll <= 0:
            return False
        return self._daily_pnl < -0.20 * self._daily_start_bankroll

    def open_position(
        self,
        trade_id: int,
        market_id: str,
        token_id: str,
        question: str,
        direction: str,
        price: float,
        size: float,
    ) -> None:
        if size > self._bankroll:
            log.warning(
                "position_size_exceeds_bankroll",
                size=size,
                bankroll=self._bankroll,
                market=question[:50],
            )
            return

        self._positions[market_id] = Position(
            trade_id=trade_id,
            market_id=market_id,
            token_id=token_id,
            question=question,
            direction=direction,
            entry_price=price,
            size=size,
            current_price=price,
            opened_at=datetime.utcnow(),
        )
        self._bankroll -= size
        log.info(
            "position_opened",
            market=question[:50],
            direction=direction,
            size=size,
            bankroll=self._bankroll,
        )

    async def close_position(self, market_id: str, exit_price: float) -> float:
        pos = self._positions.pop(market_id, None)
        if not pos:
            return 0.0

        # Calculate P&L
        shares = pos.size / pos.entry_price
        payout = shares * exit_price
        pnl = payout - pos.size

        self._bankroll += payout
        self._daily_pnl += pnl
        self._total_trades += 1
        self._daily_trades += 1

        if pnl > 0:
            self._wins += 1
            self._daily_wins += 1
            self._recent_results.append(True)
        elif pnl < 0:
            self._losses += 1
            self._daily_losses += 1
            self._daily_gross_loss += abs(pnl)
            self._recent_results.append(False)

        # Keep rolling window
        if len(self._recent_results) > self._rolling_window:
            self._recent_results = self._recent_results[-self._rolling_window:]

        # Update peak
        if self._bankroll > self._peak_bankroll:
            self._peak_bankroll = self._bankroll

        # Save to DB
        if self._trade_repo:
            await self._trade_repo.close_trade(pos.trade_id, pnl)

        log.info(
            "position_closed",
            market=pos.question[:50],
            pnl=pnl,
            bankroll=self._bankroll,
        )
        return pnl

    def update_position_price(self, market_id: str, current_price: float) -> None:
        pos = self._positions.get(market_id)
        if pos:
            pos.current_price = current_price

    def apply_reconciliation_adjustment(self, delta: float) -> None:
        """Snap bankroll to the real wallet balance AND book the difference.

        A bare `bankroll = real` (the old reconciler behaviour) silently
        erased real losses from the books — fees and slippage vanished from
        every report, so live performance looked better than it was. Here
        the delta is attributed to daily PnL so equity curves and daily
        stats stay truthful.
        """
        if delta == 0:
            return
        self._bankroll += delta
        self._daily_pnl += delta
        if self._bankroll > self._peak_bankroll:
            self._peak_bankroll = self._bankroll
        log.info(
            "reconciliation_adjustment_applied",
            delta=round(delta, 4),
            bankroll=round(self._bankroll, 4),
            daily_pnl=round(self._daily_pnl, 4),
        )

    def summary(self) -> dict:
        self._check_new_day()
        total_position_value = sum(
            p.size + p.unrealized_pnl for p in self._positions.values()
        )
        return {
            "bankroll": self._bankroll,
            "total_value": self._bankroll + total_position_value,
            "open_positions": self.open_positions_count,
            "daily_pnl": self._daily_pnl,
            "win_rate": self.win_rate,
            "total_trades": self._total_trades,
            "wins": self._wins,
            "losses": self._losses,
            "peak_bankroll": self._peak_bankroll,
            "paper_mode": self._paper_mode,
            "daily_gross_loss": self._daily_gross_loss,
        }

    async def sync_from_db(self, restore_positions: bool = True) -> None:
        if not self._trade_repo:
            return

        closed_trades = await self._trade_repo.get_closed_trades(self._paper_mode)
        self._bankroll = self._initial_bankroll + sum(t.pnl for t in closed_trades)
        self._total_trades = len(closed_trades)
        self._wins = sum(1 for t in closed_trades if t.pnl > 0)
        self._losses = sum(1 for t in closed_trades if t.pnl < 0)

        if restore_positions:
            filled_trades = await self._trade_repo.get_filled_entry_trades(self._paper_mode)
            for trade in filled_trades:
                if trade.market_id not in self._positions:
                    self._positions[trade.market_id] = Position(
                        trade_id=trade.id,
                        market_id=trade.market_id,
                        token_id=trade.token_id,
                        question=trade.question,
                        direction=trade.direction,
                        entry_price=trade.price,
                        size=trade.size,
                        current_price=trade.price,
                        opened_at=trade.created_at,
                    )
                    self._bankroll -= trade.size

        # Update peak after sync to prevent stale drawdown calculations
        if self._bankroll > self._peak_bankroll:
            self._peak_bankroll = self._bankroll

        # Restore daily stats from closed trades today
        if self._daily_stat_repo:
            try:
                today_stats = await self._daily_stat_repo.get_today()
                if today_stats:
                    self._daily_pnl = today_stats.daily_pnl
                    self._daily_trades = today_stats.trades_count
                    self._daily_wins = today_stats.wins
                    self._daily_losses = today_stats.losses
                    self._daily_start_bankroll = today_stats.starting_bankroll
                    self._peak_bankroll = max(self._peak_bankroll, today_stats.peak_bankroll)
            except Exception:
                log.debug("sync_daily_stats_skip")

        log.info(
            "portfolio_synced",
            open_positions=len(self._positions),
            bankroll=self._bankroll,
            peak_bankroll=self._peak_bankroll,
        )

    def restore_live_positions(self, positions: list[Position], bankroll: float) -> None:
        self._positions = {pos.market_id: pos for pos in positions}
        self._bankroll = bankroll
        if self._bankroll > self._peak_bankroll:
            self._peak_bankroll = self._bankroll
        log.info(
            "portfolio_live_positions_restored",
            open_positions=len(self._positions),
            bankroll=self._bankroll,
            peak_bankroll=self._peak_bankroll,
        )

    async def save_daily_stats(self) -> None:
        """Persist today's stats to the DB. Called at end of each trading cycle."""
        if not self._daily_stat_repo:
            return
        self._check_new_day()
        try:
            await self._daily_stat_repo.upsert_today(
                starting_bankroll=self._daily_start_bankroll,
                ending_bankroll=self._bankroll,
                trades_count=self._daily_trades,
                wins=self._daily_wins,
                losses=self._daily_losses,
                daily_pnl=self._daily_pnl,
                peak_bankroll=self._peak_bankroll,
            )
        except Exception:
            log.exception("save_daily_stats_error")

    def _check_new_day(self) -> None:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._last_day:
            self._daily_pnl = 0.0
            self._daily_gross_loss = 0.0
            self._daily_trades = 0
            self._daily_wins = 0
            self._daily_losses = 0
            self._daily_start_bankroll = self._bankroll
            self._last_day = today
