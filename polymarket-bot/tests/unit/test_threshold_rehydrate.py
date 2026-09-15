"""Unit tests for threshold_trader._rehydrate_position_context.

Covers the restart-resume path: after a bot restart, live positions in DB
should get their (end_date, threshold, is_above) tuples rehydrated so the
manager can close them at real endDate instead of falling to the 6h legacy
backstop.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _load_module():
    """Same trick as test_threshold_prob — stub heavy deps, import."""
    root = Path(__file__).resolve().parents[2]
    for name in ("data.binance_ws", "storage.repository", "trading.portfolio"):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            sys.modules[name] = mod
    sys.modules["data.binance_ws"].BinanceWSFeed = type("BinanceWSFeed", (), {})
    sys.modules["storage.repository"].TradeRepository = type("TradeRepository", (), {})
    sys.modules["trading.portfolio"].Portfolio = type("Portfolio", (), {})
    spec = importlib.util.spec_from_file_location(
        "threshold_trader_rehydrate_test", root / "bot" / "threshold_trader.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tt = _load_module()
ThresholdTrader = tt.ThresholdTrader


def _make_trade(market_id: str, question: str) -> SimpleNamespace:
    t = SimpleNamespace()
    t.market_id = market_id
    t.question = question
    return t


def _make_trader_instance(open_trades, held_ids, end_date_responses):
    """Build a minimal ThresholdTrader without invoking __init__."""
    trader = ThresholdTrader.__new__(ThresholdTrader)

    # Minimal state
    trader._position_context = {}
    trader._paper_mode = False

    trade_repo = MagicMock()
    trade_repo.get_open_trades = AsyncMock(return_value=open_trades)
    trader._trade_repo = trade_repo

    portfolio = SimpleNamespace()
    portfolio.held_market_ids = set(held_ids)
    trader._portfolio = portfolio

    # Stub _fetch_market_end_date to return pre-canned responses per market_id
    async def fake_fetch(cid):
        return end_date_responses.get(cid)
    trader._fetch_market_end_date = fake_fetch
    return trader


class TestRehydrate:
    @pytest.mark.asyncio
    async def test_restores_ctx_for_btc_above(self):
        t1 = _make_trade("cid_btc_1", "THRESH BTC >$80,000 | Will the price of Bitcoin be above $80,000 on April 24?")
        trader = _make_trader_instance(
            open_trades=[t1],
            held_ids={"cid_btc_1"},
            end_date_responses={"cid_btc_1": datetime(2026, 4, 24, 16, 0, tzinfo=timezone.utc)},
        )
        await trader._rehydrate_position_context()
        assert "cid_btc_1" in trader._position_context
        end_date, threshold, is_above = trader._position_context["cid_btc_1"]
        assert end_date == datetime(2026, 4, 24, 16, 0, tzinfo=timezone.utc)
        assert threshold == 80000.0
        assert is_above is True

    @pytest.mark.asyncio
    async def test_restores_eth_below(self):
        t1 = _make_trade("cid_eth", "THRESH ETH <$2,500 | Will the price of Ethereum be below $2,500")
        trader = _make_trader_instance(
            open_trades=[t1],
            held_ids={"cid_eth"},
            end_date_responses={"cid_eth": datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)},
        )
        await trader._rehydrate_position_context()
        end_date, threshold, is_above = trader._position_context["cid_eth"]
        assert threshold == 2500.0
        assert is_above is False

    @pytest.mark.asyncio
    async def test_skips_weather_trades(self):
        t1 = _make_trade("cid_weather", "WEATHER London | Will the highest temperature ...")
        trader = _make_trader_instance(
            open_trades=[t1],
            held_ids={"cid_weather"},
            end_date_responses={},
        )
        await trader._rehydrate_position_context()
        assert "cid_weather" not in trader._position_context

    @pytest.mark.asyncio
    async def test_skips_if_not_in_portfolio(self):
        t1 = _make_trade("cid_old", "THRESH BTC >$70,000 | ...")
        trader = _make_trader_instance(
            open_trades=[t1],
            held_ids=set(),  # position no longer held
            end_date_responses={"cid_old": datetime(2026, 4, 24, 16, 0, tzinfo=timezone.utc)},
        )
        await trader._rehydrate_position_context()
        assert "cid_old" not in trader._position_context

    @pytest.mark.asyncio
    async def test_skips_if_already_has_ctx(self):
        """If in-memory ctx already set (e.g. from active session), don't overwrite."""
        t1 = _make_trade("cid_btc", "THRESH BTC >$80,000 | ...")
        existing = (datetime(2026, 4, 30, 0, 0, tzinfo=timezone.utc), 80000.0, True)
        trader = _make_trader_instance(
            open_trades=[t1],
            held_ids={"cid_btc"},
            end_date_responses={"cid_btc": datetime(2026, 4, 24, 16, 0, tzinfo=timezone.utc)},
        )
        trader._position_context["cid_btc"] = existing  # pre-populate
        await trader._rehydrate_position_context()
        # Must not overwrite
        assert trader._position_context["cid_btc"] == existing

    @pytest.mark.asyncio
    async def test_populates_even_if_end_date_missing(self):
        """Gamma API failure → end_date=None, but threshold/is_above still populated.
        _manage_positions won't close on end_date but won't fall to legacy either."""
        t1 = _make_trade("cid_btc", "THRESH BTC >$80,000 | ...")
        trader = _make_trader_instance(
            open_trades=[t1],
            held_ids={"cid_btc"},
            end_date_responses={"cid_btc": None},
        )
        await trader._rehydrate_position_context()
        end_date, threshold, is_above = trader._position_context["cid_btc"]
        assert end_date is None
        assert threshold == 80000.0
        assert is_above is True

    @pytest.mark.asyncio
    async def test_skips_malformed_question(self):
        t1 = _make_trade("cid_bad", "THRESH weird format no threshold")
        trader = _make_trader_instance(
            open_trades=[t1],
            held_ids={"cid_bad"},
            end_date_responses={"cid_bad": datetime(2026, 4, 24, 16, 0, tzinfo=timezone.utc)},
        )
        await trader._rehydrate_position_context()
        assert "cid_bad" not in trader._position_context

    @pytest.mark.asyncio
    async def test_no_trade_repo_returns_early(self):
        trader = ThresholdTrader.__new__(ThresholdTrader)
        trader._position_context = {}
        trader._trade_repo = None
        trader._portfolio = SimpleNamespace(held_market_ids=set())
        trader._paper_mode = False
        await trader._rehydrate_position_context()
        # No exception; no state changes
        assert trader._position_context == {}

    @pytest.mark.asyncio
    async def test_mixed_batch_only_threshold_recovered(self):
        weather = _make_trade("cid_w", "WEATHER NYC | ...")
        threshold = _make_trade("cid_t", "THRESH SOL >$200 | ...")
        bad = _make_trade("cid_bad", "THRESH weird no dollar")
        trader = _make_trader_instance(
            open_trades=[weather, threshold, bad],
            held_ids={"cid_w", "cid_t", "cid_bad"},
            end_date_responses={"cid_t": datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc)},
        )
        await trader._rehydrate_position_context()
        assert set(trader._position_context.keys()) == {"cid_t"}
        end_date, threshold_val, is_above = trader._position_context["cid_t"]
        assert threshold_val == 200.0
        assert is_above is True
