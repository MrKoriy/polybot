from datetime import datetime

from sqlalchemy import select, update

from storage.database import get_session
from storage.models import DailyStat, Market, Prediction, RiskEvent, Trade


class TradeRepository:
    async def create_trade(self, **kwargs) -> Trade:
        async with get_session() as session:
            trade = Trade(**kwargs)
            session.add(trade)
            await session.commit()
            await session.refresh(trade)
            return trade

    async def get_open_trades(self, paper_mode: bool = True) -> list[Trade]:
        async with get_session() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.status.in_(["open", "filled"]),
                    Trade.paper_mode == paper_mode,
                )
            )
            return list(result.scalars().all())

    async def get_active_entry_trades(self, paper_mode: bool = True) -> list[Trade]:
        async with get_session() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.status.in_(["open", "filled"]),
                    Trade.paper_mode == paper_mode,
                    Trade.side == "BUY",
                )
            )
            return list(result.scalars().all())

    async def get_filled_entry_trades(self, paper_mode: bool = True) -> list[Trade]:
        async with get_session() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.status == "filled",
                    Trade.paper_mode == paper_mode,
                    Trade.side == "BUY",
                )
            )
            return list(result.scalars().all())

    async def close_trade(self, trade_id: int, pnl: float) -> None:
        async with get_session() as session:
            await session.execute(
                update(Trade)
                .where(Trade.id == trade_id)
                .values(status="closed", pnl=pnl, closed_at=datetime.utcnow())
            )
            await session.commit()

    async def update_trade_status(
        self,
        trade_id: int,
        status: str,
        fill_price: float = 0.0,
        fill_size: float = 0.0,
    ) -> None:
        values: dict = {"status": status}
        if fill_price > 0:
            values["price"] = fill_price
        if fill_size > 0:
            values["size"] = fill_size
        async with get_session() as session:
            await session.execute(
                update(Trade).where(Trade.id == trade_id).values(**values)
            )
            await session.commit()

    async def get_trades_today(self, paper_mode: bool = True) -> list[Trade]:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        async with get_session() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.created_at >= today,
                    Trade.paper_mode == paper_mode,
                )
            )
            return list(result.scalars().all())

    async def get_closed_trades(self, paper_mode: bool = True) -> list[Trade]:
        async with get_session() as session:
            result = await session.execute(
                select(Trade).where(
                    Trade.status == "closed",
                    Trade.paper_mode == paper_mode,
                    Trade.side == "BUY",
                )
            )
            return list(result.scalars().all())

    async def get_recent_trades(self, limit: int = 10) -> list[Trade]:
        async with get_session() as session:
            result = await session.execute(
                select(Trade).order_by(Trade.created_at.desc()).limit(limit)
            )
            return list(result.scalars().all())


class MarketRepository:
    async def upsert_market(self, condition_id: str, **kwargs) -> Market:
        async with get_session() as session:
            result = await session.execute(
                select(Market).where(Market.condition_id == condition_id)
            )
            market = result.scalar_one_or_none()
            if market:
                for k, v in kwargs.items():
                    setattr(market, k, v)
                market.last_seen_at = datetime.utcnow()
            else:
                market = Market(condition_id=condition_id, **kwargs)
                session.add(market)
            await session.commit()
            await session.refresh(market)
            return market


class PredictionRepository:
    async def create_prediction(self, **kwargs) -> Prediction:
        async with get_session() as session:
            pred = Prediction(**kwargs)
            session.add(pred)
            await session.commit()
            await session.refresh(pred)
            return pred

    async def get_unresolved_predictions(self) -> list[Prediction]:
        async with get_session() as session:
            result = await session.execute(
                select(Prediction).where(Prediction.actual_outcome.is_(None))
            )
            return list(result.scalars().all())

    async def get_unresolved_for_market(self, market_id: str) -> list[Prediction]:
        async with get_session() as session:
            result = await session.execute(
                select(Prediction).where(
                    Prediction.market_id == market_id,
                    Prediction.actual_outcome.is_(None),
                )
            )
            return list(result.scalars().all())

    async def resolve_prediction(self, prediction_id: int, actual_outcome: float) -> None:
        async with get_session() as session:
            await session.execute(
                update(Prediction)
                .where(Prediction.id == prediction_id)
                .values(actual_outcome=actual_outcome, resolved_at=datetime.utcnow())
            )
            await session.commit()

    async def get_resolved_predictions(self, limit: int = 100) -> list[Prediction]:
        async with get_session() as session:
            result = await session.execute(
                select(Prediction)
                .where(Prediction.actual_outcome.is_not(None))
                .order_by(Prediction.resolved_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())


class DailyStatRepository:
    async def get_today(self) -> DailyStat | None:
        """Get today's stats for sync on restart."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        async with get_session() as session:
            result = await session.execute(
                select(DailyStat).where(DailyStat.date == today)
            )
            return result.scalar_one_or_none()

    async def upsert_today(self, **kwargs) -> DailyStat:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        async with get_session() as session:
            result = await session.execute(
                select(DailyStat).where(DailyStat.date == today)
            )
            stat = result.scalar_one_or_none()
            if stat:
                for k, v in kwargs.items():
                    setattr(stat, k, v)
            else:
                stat = DailyStat(date=today, **kwargs)
                session.add(stat)
            await session.commit()
            await session.refresh(stat)
            return stat


class RiskEventRepository:
    async def create_event(self, event_type: str, details: str = "") -> RiskEvent:
        async with get_session() as session:
            event = RiskEvent(event_type=event_type, details=details)
            session.add(event)
            await session.commit()
            return event
