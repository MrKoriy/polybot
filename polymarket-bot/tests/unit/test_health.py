import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from bot.health import HealthChecker
from data.polymarket_rest import PolymarketClient


class DummyNewsSource:
    name = "dummy_news"

    async def health_check(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_health_checker_uses_gamma_probe_not_filtered_market_candidates():
    polymarket = MagicMock()
    polymarket.gamma_health_check = AsyncMock(return_value=True)
    polymarket.get_active_markets = AsyncMock(return_value=[])

    checker = HealthChecker(polymarket=polymarket, news_sources=[DummyNewsSource()])
    result = await checker.check_all()

    assert result["polymarket_gamma"] is True
    assert result["healthy"] is True
    polymarket.gamma_health_check.assert_awaited_once()
    polymarket.get_active_markets.assert_not_called()


@pytest.mark.asyncio
async def test_gamma_health_check_accepts_any_valid_market_list():
    client = PolymarketClient()
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = []
    client._http.get = AsyncMock(return_value=response)

    assert await client.gamma_health_check() is True


@pytest.mark.asyncio
async def test_gamma_health_check_returns_false_on_invalid_json_shape():
    client = PolymarketClient()
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"error": "unexpected"}
    client._http.get = AsyncMock(return_value=response)

    assert await client.gamma_health_check() is False


@pytest.mark.asyncio
async def test_get_midpoint_normalizes_sdk_dict_response():
    client = PolymarketClient()
    client._clob_client = MagicMock()
    client._clob_client.get_midpoint.return_value = {"mid": "0.42"}

    assert await client.get_midpoint("123") == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_get_conditional_balance_verifies_clob_zero_with_rpc():
    client = PolymarketClient()
    client._get_conditional_balance_clob = AsyncMock(return_value=0.0)
    client._get_conditional_balance_rpc = AsyncMock(return_value=6.25)

    assert await client.get_conditional_balance("123") == pytest.approx(6.25)
    client._get_conditional_balance_rpc.assert_awaited_once_with("123")


@pytest.mark.asyncio
async def test_conditional_balance_rpc_uses_polygon_rpc_fallback_helper():
    client = PolymarketClient(funder_address="0x0000000000000000000000000000000000000123")
    client._clob_client = MagicMock()
    client._clob_client.get_conditional_address.return_value = (
        "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    )
    client._polygon_rpc = AsyncMock(return_value=hex(2_500_000))

    assert await client._get_conditional_balance_rpc("123") == pytest.approx(2.5)
    client._polygon_rpc.assert_awaited_once()
