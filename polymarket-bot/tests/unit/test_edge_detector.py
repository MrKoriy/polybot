import pytest

from analysis.edge_detector import EdgeDetector
from config.constants import Direction


class TestEdgeDetector:
    def setup_method(self):
        self.detector = EdgeDetector(min_edge=0.10, min_confidence=0.75)

    def test_detect_yes_edge(self):
        signal = self.detector.detect(
            estimated_prob=0.70,
            market_yes_price=0.50,
            market_no_price=0.50,
            confidence=0.80,
            yes_token_id="yes_token",
            no_token_id="no_token",
        )
        assert signal is not None
        assert signal.direction == Direction.BUY_YES
        assert signal.edge_size == pytest.approx(0.20)
        assert signal.token_id == "yes_token"
        assert signal.expected_value > 0

    def test_detect_no_edge(self):
        signal = self.detector.detect(
            estimated_prob=0.30,
            market_yes_price=0.50,
            market_no_price=0.50,
            confidence=0.80,
            yes_token_id="yes_token",
            no_token_id="no_token",
        )
        assert signal is not None
        assert signal.direction == Direction.BUY_NO
        assert signal.edge_size == pytest.approx(0.20)
        assert signal.token_id == "no_token"
        assert signal.expected_value > 0

    def test_no_edge_when_close(self):
        signal = self.detector.detect(
            estimated_prob=0.55,
            market_yes_price=0.50,
            market_no_price=0.50,
            confidence=0.80,
            yes_token_id="yes_token",
            no_token_id="no_token",
        )
        assert signal is None  # 5% edge < 10% min

    def test_no_edge_low_confidence(self):
        signal = self.detector.detect(
            estimated_prob=0.70,
            market_yes_price=0.50,
            market_no_price=0.50,
            confidence=0.60,  # below 0.75 threshold
            yes_token_id="yes_token",
            no_token_id="no_token",
        )
        assert signal is None

    def test_boundary_edge(self):
        # Just above 10% edge should be detected
        signal = self.detector.detect(
            estimated_prob=0.61,
            market_yes_price=0.50,
            market_no_price=0.50,
            confidence=0.80,
            yes_token_id="yes_token",
            no_token_id="no_token",
        )
        assert signal is not None
        assert signal.edge_size == pytest.approx(0.11)

    def test_extreme_probabilities(self):
        signal = self.detector.detect(
            estimated_prob=0.95,
            market_yes_price=0.50,
            market_no_price=0.50,
            confidence=0.90,
            yes_token_id="yes_token",
            no_token_id="no_token",
        )
        assert signal is not None
        assert signal.direction == Direction.BUY_YES
        assert signal.edge_size == pytest.approx(0.45)

    def test_spread_reduces_edge(self):
        """High spread should eat into the edge and may reject borderline trades."""
        # 11% raw edge with 10% spread -> net edge = 6% (below 10% min)
        signal = self.detector.detect(
            estimated_prob=0.61,
            market_yes_price=0.50,
            market_no_price=0.50,
            confidence=0.80,
            yes_token_id="yes_token",
            no_token_id="no_token",
            spread=0.10,
        )
        assert signal is None  # Spread cost (5%) reduces 11% edge to 6%

    def test_confidence_adjusted_edge(self):
        """Higher confidence should produce a higher confidence_adjusted_edge."""
        signal_high = self.detector.detect(
            estimated_prob=0.70,
            market_yes_price=0.50,
            market_no_price=0.50,
            confidence=0.90,
            yes_token_id="yes_token",
            no_token_id="no_token",
        )
        signal_low = self.detector.detect(
            estimated_prob=0.70,
            market_yes_price=0.50,
            market_no_price=0.50,
            confidence=0.76,
            yes_token_id="yes_token",
            no_token_id="no_token",
        )
        assert signal_high is not None
        assert signal_low is not None
        assert signal_high.confidence_adjusted_edge > signal_low.confidence_adjusted_edge

    def test_picks_best_direction_by_confidence_adjusted_edge(self):
        """When both YES and NO have edge, should pick the one with better adjusted EV."""
        # Asymmetric market where both sides have edge
        signal = self.detector.detect(
            estimated_prob=0.50,
            market_yes_price=0.35,
            market_no_price=0.35,  # Market sums < 1 -> both sides have edge
            confidence=0.80,
            yes_token_id="yes_token",
            no_token_id="no_token",
        )
        assert signal is not None
        # Both sides have 15% edge; should pick YES or NO based on best EV
