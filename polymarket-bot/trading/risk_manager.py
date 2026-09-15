from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

from analysis.market_filter import classify_market_question
from config.constants import RiskEventType
from storage.repository import RiskEventRepository

log = structlog.get_logger()


@dataclass
class TradeProposal:
    market_id: str
    token_id: str
    question: str
    direction: str
    price: float
    size: float
    edge: float
    estimated_prob: float
    confidence: float
    kelly_fraction: float
    reasoning: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeApproval:
    approved: bool
    reason: str = ""
    proposal: TradeProposal | None = None


class RiskManager:
    def __init__(
        self,
        max_position_pct: float = 0.20,
        max_open_positions_small: int = 3,
        max_open_positions_large: int = 5,
        daily_loss_limit: float = 0.30,
        drawdown_pause: float = 0.50,
        min_edge: float = 0.10,
        min_confidence: float = 0.75,
        min_market_volume: float = 5000,
        max_category_exposure_pct: float = 0.50,
        risk_repo: RiskEventRepository | None = None,
    ) -> None:
        self._max_position_pct = max_position_pct
        self._max_positions_small = max_open_positions_small
        self._max_positions_large = max_open_positions_large
        self._daily_loss_limit = daily_loss_limit
        self._drawdown_pause = drawdown_pause
        self._min_edge = min_edge
        self._min_confidence = min_confidence
        self._min_volume = min_market_volume
        self._max_category_exposure_pct = max_category_exposure_pct
        self._risk_repo = risk_repo
        self._paused_until: datetime | None = None

    @property
    def is_paused(self) -> bool:
        if self._paused_until and datetime.utcnow() < self._paused_until:
            return True
        self._paused_until = None
        return False

    async def approve(
        self,
        proposal: TradeProposal,
        bankroll: float,
        peak_bankroll: float,
        daily_pnl: float,
        open_positions_count: int,
        held_market_ids: set[str],
        market_volume: float = 0,
        open_positions: list | None = None,
    ) -> TradeApproval:
        # Check if paused
        if self.is_paused:
            return TradeApproval(False, "Trading paused due to drawdown")

        # Edge check
        if proposal.edge < self._min_edge:
            return await self._reject(
                proposal, RiskEventType.EDGE_TOO_SMALL,
                f"Edge {proposal.edge:.3f} < min {self._min_edge}"
            )

        # Confidence check
        if proposal.confidence < self._min_confidence:
            return await self._reject(
                proposal, RiskEventType.LOW_CONFIDENCE,
                f"Confidence {proposal.confidence:.2f} < min {self._min_confidence}"
            )

        # Daily loss limit
        if bankroll > 0 and daily_pnl < 0 and abs(daily_pnl) / bankroll > self._daily_loss_limit:
            return await self._reject(
                proposal, RiskEventType.DAILY_LOSS_LIMIT,
                f"Daily loss {daily_pnl:.2f} exceeds {self._daily_loss_limit*100:.0f}% limit"
            )

        # Drawdown from peak — adaptive pause proportional to severity
        if peak_bankroll > 0:
            drawdown = (peak_bankroll - bankroll) / peak_bankroll
            if drawdown > self._drawdown_pause:
                # Adaptive: 1h for 50% DD, 2h for 60%, 4h for 70%+
                pause_hours = min(4.0, 1.0 * (2 ** ((drawdown - self._drawdown_pause) / 0.10)))
                self._paused_until = datetime.utcnow() + timedelta(hours=pause_hours)
                return await self._reject(
                    proposal, RiskEventType.DRAWDOWN_PAUSE,
                    f"Drawdown {drawdown*100:.1f}% > {self._drawdown_pause*100:.0f}% — pausing {pause_hours:.1f}h"
                )

        # Position count
        max_positions = (
            self._max_positions_large if bankroll >= 50 else self._max_positions_small
        )
        if open_positions_count >= max_positions:
            return await self._reject(
                proposal, RiskEventType.MAX_POSITIONS,
                f"{open_positions_count} open positions >= max {max_positions}"
            )

        # Already in this market
        if proposal.market_id in held_market_ids:
            return TradeApproval(False, f"Already holding position in {proposal.market_id[:20]}")

        # Category exposure check — prevent over-concentration in one market type
        if open_positions and bankroll > 0:
            new_category = classify_market_question(proposal.question)
            category_exposure = proposal.size
            for pos in open_positions:
                pos_category = classify_market_question(getattr(pos, "question", ""))
                if pos_category == new_category:
                    category_exposure += getattr(pos, "size", 0)
            if category_exposure / bankroll > self._max_category_exposure_pct:
                return await self._reject(
                    proposal, RiskEventType.MAX_POSITIONS,
                    f"Category '{new_category}' exposure ${category_exposure:.2f} > {self._max_category_exposure_pct*100:.0f}% of bankroll"
                )

        # Position size
        if bankroll > 0 and proposal.size / bankroll > self._max_position_pct:
            return await self._reject(
                proposal, RiskEventType.INSUFFICIENT_BALANCE,
                f"Position size ${proposal.size:.2f} > {self._max_position_pct*100:.0f}% of bankroll"
            )

        # Balance check
        if proposal.size > bankroll:
            return await self._reject(
                proposal, RiskEventType.INSUFFICIENT_BALANCE,
                f"Insufficient balance: need ${proposal.size:.2f}, have ${bankroll:.2f}"
            )

        # Liquidity check
        if market_volume > 0 and market_volume < self._min_volume:
            return await self._reject(
                proposal, RiskEventType.LOW_LIQUIDITY,
                f"Market volume ${market_volume:.0f} < min ${self._min_volume:.0f}"
            )

        log.info(
            "trade_approved",
            market=proposal.question[:50],
            size=proposal.size,
            edge=proposal.edge,
        )
        return TradeApproval(True, proposal=proposal)

    async def _reject(
        self, proposal: TradeProposal, event_type: RiskEventType, reason: str
    ) -> TradeApproval:
        log.info("trade_rejected", reason=reason, market=proposal.question[:50])
        if self._risk_repo:
            await self._risk_repo.create_event(
                event_type=event_type.value,
                details=f"{reason} | market={proposal.question[:100]}",
            )
        return TradeApproval(False, reason)

    def sync_thresholds(self, settings) -> None:
        """Sync risk thresholds from Settings — call each cycle so AutoResearch changes take effect."""
        self._min_edge = settings.min_edge
        self._min_confidence = settings.min_confidence
        self._min_volume = settings.min_market_volume
        self._max_position_pct = settings.max_position_pct

    def pause(self, hours: float = 1.0) -> None:
        self._paused_until = datetime.utcnow() + timedelta(hours=hours)
        log.warning("trading_paused", until=self._paused_until.isoformat())

    def resume(self) -> None:
        self._paused_until = None
        log.info("trading_resumed")
