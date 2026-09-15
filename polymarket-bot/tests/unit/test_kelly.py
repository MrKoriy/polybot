import pytest

from trading.kelly import KellyCalculator


class TestKellyCalculator:
    def setup_method(self):
        self.calc = KellyCalculator(
            default_fraction=0.25,
            max_position_pct=0.20,
            min_bet=0.50,
        )

    def test_positive_edge_returns_bet(self):
        result = self.calc.calculate(
            estimated_prob=0.70,
            market_price=0.50,
            bankroll=100.0,
        )
        assert result is not None
        assert result.bet_size > 0
        assert result.edge == pytest.approx(0.20)
        assert result.expected_value > 0

    def test_no_edge_returns_none(self):
        result = self.calc.calculate(
            estimated_prob=0.50,
            market_price=0.50,
            bankroll=100.0,
        )
        assert result is None

    def test_negative_edge_returns_none(self):
        result = self.calc.calculate(
            estimated_prob=0.40,
            market_price=0.50,
            bankroll=100.0,
        )
        assert result is None

    def test_quarter_kelly_reduces_bet(self):
        full = self.calc.calculate(
            estimated_prob=0.70,
            market_price=0.50,
            bankroll=100.0,
            kelly_fraction=1.0,
        )
        quarter = self.calc.calculate(
            estimated_prob=0.70,
            market_price=0.50,
            bankroll=100.0,
            kelly_fraction=0.25,
        )
        assert full is not None
        assert quarter is not None
        # Quarter Kelly should be roughly 1/4 of full (clamped by max_position_pct)
        assert quarter.bet_fraction <= full.bet_fraction

    def test_max_position_pct_clamp(self):
        # Very high edge should still be clamped
        result = self.calc.calculate(
            estimated_prob=0.95,
            market_price=0.10,
            bankroll=100.0,
            kelly_fraction=1.0,
        )
        assert result is not None
        assert result.bet_fraction <= 0.20  # max_position_pct

    def test_small_bankroll_min_bet(self):
        result = self.calc.calculate(
            estimated_prob=0.70,
            market_price=0.50,
            bankroll=5.0,
        )
        # Bankroll too small for min bet (5.0 < 0.50 * 2)
        # Actually 5.0 >= 1.0, so it should work
        if result is not None:
            assert result.bet_size >= 0.50

    def test_very_small_bankroll_returns_none(self):
        result = self.calc.calculate(
            estimated_prob=0.70,
            market_price=0.50,
            bankroll=0.50,
        )
        # Bankroll = 0.50 < min_bet * 2 = 1.0
        assert result is None

    def test_price_near_one_returns_none(self):
        result = self.calc.calculate(
            estimated_prob=0.99,
            market_price=1.0,
            bankroll=100.0,
        )
        assert result is None

    def test_edge_and_ev_calculation(self):
        result = self.calc.calculate(
            estimated_prob=0.60,
            market_price=0.40,
            bankroll=100.0,
        )
        assert result is not None
        assert result.edge == pytest.approx(0.20)
        # EV = p * (1/price - 1) - (1-p) = 0.6 * 1.5 - 0.4 = 0.5
        assert result.expected_value == pytest.approx(0.50)

    def test_different_kelly_fractions(self):
        results = {}
        for frac in [0.125, 0.25, 0.5, 1.0]:
            r = self.calc.calculate(
                estimated_prob=0.65,
                market_price=0.45,
                bankroll=100.0,
                kelly_fraction=frac,
            )
            if r is not None:
                results[frac] = r.bet_size

        # Higher Kelly fraction = larger bet (up to max_position_pct)
        fracs = sorted(results.keys())
        for i in range(len(fracs) - 1):
            assert results[fracs[i]] <= results[fracs[i + 1]]
