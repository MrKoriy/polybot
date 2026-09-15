"""Unit tests for EnsembleProbabilityEstimator: ensemble logic and bias correction."""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from analysis.llm_client import LLMClient, ProbabilityEstimate
from analysis.probability_estimator import EnsembleProbabilityEstimator
from config.constants import LLMProvider
from core.exceptions import LLMError
from data.base import NewsItem


def _make_news(n: int = 2) -> list[NewsItem]:
    now = datetime.now(timezone.utc)
    return [
        NewsItem(
            title=f"News {i}", body=f"Body {i}",
            source="test", url=f"https://example.com/{i}",
            published_at=now - timedelta(minutes=i * 5),
        )
        for i in range(n)
    ]


def _make_llm_estimate(probability: float, confidence: float = 0.80) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        probability=probability,
        confidence=confidence,
        reasoning="Test reasoning",
        key_factors=["factor1"],
        counter_arguments=["counter1"],
        model_used="test-model",
    )


def _make_estimator(
    primary_prob: float = 0.70,
    primary_conf: float = 0.80,
    secondary_prob: float = 0.72,
    secondary_conf: float = 0.78,
    use_ensemble: bool = True,
    min_edge: float = 0.08,
    min_confidence: float = 0.70,
    bias_correction: float = 0.04,
    raise_primary: bool = False,
    raise_secondary: bool = False,
):
    mock_llm = MagicMock(spec=LLMClient)

    if raise_primary:
        mock_llm.estimate_probability = AsyncMock(side_effect=LLMError("Primary LLM error"))
    elif raise_secondary:
        # First call succeeds (primary), second raises (secondary)
        mock_llm.estimate_probability = AsyncMock(
            side_effect=[
                _make_llm_estimate(primary_prob, primary_conf),
                LLMError("Secondary LLM error"),
            ]
        )
    else:
        mock_llm.estimate_probability = AsyncMock(
            side_effect=[
                _make_llm_estimate(primary_prob, primary_conf),
                _make_llm_estimate(secondary_prob, secondary_conf),
            ]
        )

    estimator = EnsembleProbabilityEstimator(
        llm_client=mock_llm,
        bias_correction=bias_correction,
        min_edge=min_edge,
        min_confidence=min_confidence,
        use_ensemble=use_ensemble,
        primary_provider=LLMProvider.OPENROUTER,
        secondary_provider=LLMProvider.OPENROUTER,
        primary_model="primary-model",
        secondary_model="secondary-model",
    )
    return estimator, mock_llm


class TestBiasCorrection:
    async def test_positive_bias_correction_reduces_estimate(self):
        """When estimate > market price, bias correction pushes it down."""
        # Primary says 0.70, market is 0.50. Corrected = 0.70 - 0.04 = 0.66
        # Edge = 0.66 - 0.50 = 0.16 > min_edge=0.08 → passes
        estimator, _ = _make_estimator(
            primary_prob=0.70, secondary_prob=0.68,
            bias_correction=0.04,
        )
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        assert result is not None
        # Final prob should be below raw estimate
        assert result.probability < 0.70

    async def test_negative_bias_correction_increases_estimate(self):
        """When estimate < market price, bias correction pushes it up."""
        # Primary says 0.30, market is 0.50. Corrected = 0.30 + 0.04 = 0.34
        # Edge on NO = (1 - 0.34) - 0.50 = 0.16 > min_edge → passes
        estimator, _ = _make_estimator(
            primary_prob=0.30, secondary_prob=0.28,
            bias_correction=0.04,
        )
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        assert result is not None
        # Final prob should be above raw estimate (corrected toward market)
        assert result.probability > 0.30

    async def test_bias_correction_clamped_to_bounds(self):
        """Bias correction never pushes probability outside [0.01, 0.99]."""
        # Extreme estimate: 0.02, bias pushes toward market (0.50) → 0.02 + 0.04 = 0.06
        estimator, _ = _make_estimator(
            primary_prob=0.02, secondary_prob=0.03,
            bias_correction=0.04, min_edge=0.04,
        )
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        if result:
            assert result.probability >= 0.01
            assert result.probability <= 0.99


class TestEnsembleLogic:
    async def test_ensemble_averages_estimates(self):
        """Final probability is confidence-weighted average of corrected primary and secondary."""
        # primary=0.70, market=0.50, divergence=0.20
        # Proportional bias correction reduces divergence by ~12% (0.04*3=0.12)
        # raw=0.70, market=0.50, divergence=0.20, correction=0.20*0.12=0.024
        # corrected ≈ 0.70 - 0.024 = 0.676
        # Ensemble average of two identical estimates = same value
        estimator, _ = _make_estimator(
            primary_prob=0.70, primary_conf=0.80,
            secondary_prob=0.70, secondary_conf=0.80,
            bias_correction=0.04, use_ensemble=True,
        )
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        assert result is not None
        assert len(result.models_used) == 2
        # Bias-corrected estimate should be between raw (0.70) and market (0.50)
        assert 0.55 < result.probability < 0.70

    async def test_ensemble_disagreement_slashes_confidence(self):
        """When primary and secondary disagree on direction, confidence is heavily penalized."""
        # Primary: 0.70 (corrected=0.64), above market 0.50 → YES edge
        # Secondary: 0.35 (corrected ~0.39), below market 0.50 → NO edge
        # Disagreement → use primary but slash confidence to 40%
        # 0.80 * 0.40 = 0.32 < min_confidence 0.70 → returns None
        estimator, _ = _make_estimator(
            primary_prob=0.70, primary_conf=0.80,
            secondary_prob=0.35, secondary_conf=0.80,
            bias_correction=0.04, use_ensemble=True,
            min_confidence=0.70,
        )
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        # With 0.80 * 0.40 = 0.32 confidence, below min_confidence → None
        assert result is None

    async def test_single_model_mode(self):
        """With use_ensemble=False, only one LLM call is made."""
        estimator, mock_llm = _make_estimator(
            primary_prob=0.75, primary_conf=0.80,
            use_ensemble=False,
        )
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        assert result is not None
        assert mock_llm.estimate_probability.call_count == 1
        assert len(result.models_used) == 1

    async def test_secondary_llm_failure_falls_back_to_primary(self):
        """If secondary LLM fails, fallback confidence is 80% of primary.
        With primary_conf=0.95 → fallback=0.76 > min_confidence=0.70 → estimate returned."""
        estimator, _ = _make_estimator(
            primary_prob=0.75, primary_conf=0.95,
            raise_secondary=True,
            use_ensemble=True,
            min_confidence=0.70,
        )
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        assert result is not None
        # Only primary model used (secondary failed)
        assert len(result.models_used) == 1
        # Confidence is reduced from 0.95 to 0.95 * 0.8 = 0.76
        assert result.confidence < 0.95

    async def test_primary_llm_failure_returns_none(self):
        """If primary LLM fails entirely, estimate returns None."""
        estimator, _ = _make_estimator(raise_primary=True)
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        assert result is None


class TestEdgeFiltering:
    async def test_no_edge_returns_none(self):
        """When estimate is close to market price (no edge), returns None."""
        # primary=0.52 (corrected=0.48), market=0.50 → edge=0.02 < min_edge=0.08
        estimator, _ = _make_estimator(
            primary_prob=0.52, primary_conf=0.80,
            min_edge=0.08,
        )
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        assert result is None

    async def test_low_confidence_returns_none(self):
        """When confidence is below threshold, returns None."""
        # Good edge but low confidence
        estimator, _ = _make_estimator(
            primary_prob=0.70, primary_conf=0.50,
            min_confidence=0.70,
        )
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        assert result is None

    async def test_strong_edge_returns_estimate(self):
        """Strong signal well above thresholds returns an estimate."""
        estimator, _ = _make_estimator(
            primary_prob=0.80, primary_conf=0.90,
            secondary_prob=0.82, secondary_conf=0.88,
            bias_correction=0.02,
            min_edge=0.08, min_confidence=0.70,
        )
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        assert result is not None
        assert result.confidence > 0.70

    async def test_update_thresholds(self):
        """update_thresholds changes min_edge and min_confidence dynamically."""
        estimator, _ = _make_estimator(
            primary_prob=0.70, primary_conf=0.80,
            min_edge=0.08, min_confidence=0.70,
        )

        # After raising min_confidence to 0.90, this estimate should fail
        estimator.update_thresholds(min_edge=0.08, min_confidence=0.90, bias_correction=0.04)
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        assert result is None  # confidence=0.80 < updated threshold=0.90

    async def test_factors_and_counters_deduplicated(self):
        """key_factors and counter_arguments are deduplicated across models."""
        estimator, mock_llm = _make_estimator(
            primary_prob=0.70, primary_conf=0.80,
            secondary_prob=0.72, secondary_conf=0.78,
            use_ensemble=True,
        )
        # Make both models return the same factors
        mock_llm.estimate_probability.side_effect = [
            ProbabilityEstimate(0.70, 0.80, "r1", ["factorA", "factorB"], ["c1"], "m1"),
            ProbabilityEstimate(0.72, 0.78, "r2", ["factorA", "factorC"], ["c1"], "m2"),
        ]
        result = await estimator.estimate(
            question="Test?", market_price=0.50,
            end_date="2026-03-19", news_items=_make_news(),
        )
        if result:
            # No duplicate factors
            assert len(result.key_factors) == len(set(result.key_factors))
            assert len(result.counter_arguments) == len(set(result.counter_arguments))
