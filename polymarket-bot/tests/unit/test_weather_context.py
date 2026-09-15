from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.weather_trader import WeatherTrader


class _PortfolioStub:
    def __init__(self, held_market_ids):
        self.held_market_ids = held_market_ids


def _trade(**overrides):
    base = {
        "market_id": "m1",
        "question": "WEATHER Tokyo | Will the highest temperature in Tok",
        "direction": "BUY_YES",
        "reasoning": "fcst=23.3°C σ=1.25 bucket=24°C [23.0,25.0] | target=2026-04-21",
        "created_at": datetime(2026, 4, 21, 6, 1, 8, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_rehydrate_weather_context_from_open_trade() -> None:
    trade_repo = SimpleNamespace(
        get_open_trades=AsyncMock(return_value=[_trade()]),
    )
    portfolio = _PortfolioStub({"m1"})
    trader = WeatherTrader(portfolio=portfolio, trade_repo=trade_repo)

    await trader._rehydrate_position_context()

    assert trader._pos_ctx["m1"] == {
        "city": "tokyo",
        "target_date": "2026-04-21",
        "side": "YES",
        "mtype": "temp_exact",
        "threshold_c": 24.0,
    }

    await trader._http.aclose()


def test_infer_target_date_from_legacy_trade_timestamp() -> None:
    trade = _trade(reasoning="fcst=17.5°C σ=1.12 bucket=14°C [13.0,15.0]")

    inferred = WeatherTrader._infer_target_date(trade)

    assert inferred == "2026-04-21"
