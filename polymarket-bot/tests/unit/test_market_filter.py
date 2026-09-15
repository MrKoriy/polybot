from datetime import datetime, timedelta

import pytest

from analysis.market_filter import MarketFilter
from data.base import MarketCandidate


def make_market(**overrides) -> MarketCandidate:
    end = datetime.utcnow() + timedelta(days=3)
    defaults = {
        "condition_id": "cond_1",
        "question": "Will X happen?",
        "yes_token_id": "yes_1",
        "no_token_id": "no_1",
        "yes_price": 0.50,
        "no_price": 0.50,
        "volume": 10000,
        "volume_24h": 5000,
        "end_date": end.isoformat() + "Z",
        "spread": 0.02,
    }
    defaults.update(overrides)
    return MarketCandidate(**defaults)


class TestMarketFilter:
    def setup_method(self):
        self.filter = MarketFilter(
            min_volume=5000, max_days_to_resolution=7, max_spread=0.15, top_n=10
        )

    def test_passes_valid_market(self):
        markets = [make_market()]
        result = self.filter.filter(markets)
        assert len(result) == 1

    def test_filters_low_volume(self):
        markets = [make_market(volume=1000)]
        result = self.filter.filter(markets)
        assert len(result) == 0

    def test_filters_high_spread(self):
        markets = [make_market(spread=0.20)]
        result = self.filter.filter(markets)
        assert len(result) == 0

    def test_filters_distant_resolution(self):
        end = datetime.utcnow() + timedelta(days=30)
        markets = [make_market(end_date=end.isoformat() + "Z")]
        result = self.filter.filter(markets)
        assert len(result) == 0

    def test_filters_past_resolution(self):
        end = datetime.utcnow() - timedelta(days=1)
        markets = [make_market(end_date=end.isoformat() + "Z")]
        result = self.filter.filter(markets)
        assert len(result) == 0

    def test_filters_extreme_prices(self):
        markets = [make_market(yes_price=0.98)]
        result = self.filter.filter(markets)
        assert len(result) == 0

    def test_filters_held_markets(self):
        markets = [make_market(condition_id="cond_1")]
        result = self.filter.filter(markets, held_market_ids={"cond_1"})
        assert len(result) == 0

    def test_sorts_by_volume_24h(self):
        markets = [
            make_market(condition_id="low", volume_24h=1000),
            make_market(condition_id="high", volume_24h=9000),
            make_market(condition_id="mid", volume_24h=5000),
        ]
        result = self.filter.filter(markets)
        assert len(result) == 3
        assert result[0].condition_id == "high"
        assert result[1].condition_id == "mid"
        assert result[2].condition_id == "low"

    def test_top_n_limit(self):
        markets = [make_market(condition_id=f"m_{i}") for i in range(20)]
        result = self.filter.filter(markets)
        assert len(result) == 10  # top_n=10
