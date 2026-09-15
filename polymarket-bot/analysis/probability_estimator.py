from dataclasses import dataclass

import structlog

from analysis.calibration_tracker import CalibrationTracker
from analysis.llm_client import LLMClient, ProbabilityEstimate
from config.constants import LLMProvider
from core.exceptions import LLMError
from data.base import NewsItem

log = structlog.get_logger()


@dataclass
class EnsembleEstimate:
    probability: float
    confidence: float
    reasoning: str
    key_factors: list[str]
    counter_arguments: list[str]
    models_used: list[str]
    raw_estimates: list[ProbabilityEstimate]
    bias_corrected: bool = True
    news_freshness_score: float = 0.0  # 0-1, how fresh the news driving this estimate is


class EnsembleProbabilityEstimator:
    """Two-stage ensemble: cheap model screens, expensive model confirms.

    Improvements over v1:
    - Weighted ensemble averaging (confidence-weighted, not min)
    - Dynamic bias correction based on calibration history
    - News freshness scoring to gauge how actionable the edge is
    - Disagreement yields reduced confidence instead of hard rejection
    """

    # How often (in calls to update_bias_from_calibration) to refresh bias correction.
    _CALIBRATION_UPDATE_INTERVAL = 20

    def __init__(
        self,
        llm_client: LLMClient,
        bias_correction: float = 0.04,
        min_edge: float = 0.10,
        min_confidence: float = 0.75,
        use_ensemble: bool = True,
        primary_provider: LLMProvider = LLMProvider.OPENROUTER,
        secondary_provider: LLMProvider = LLMProvider.OPENROUTER,
        primary_model: str = "google/gemini-3.1-flash-lite-preview",
        secondary_model: str = "google/gemini-3.1-flash-lite-preview",
        calibration: CalibrationTracker | None = None,
    ) -> None:
        self._llm = llm_client
        self._bias_correction = bias_correction
        self._min_edge = min_edge
        self._min_confidence = min_confidence
        self._use_ensemble = use_ensemble
        self._primary_provider = primary_provider
        self._secondary_provider = secondary_provider
        self._primary_model = primary_model
        self._secondary_model = secondary_model
        self._calibration = calibration
        self._estimates_since_calibration = 0

    def update_thresholds(self, min_edge: float, min_confidence: float, bias_correction: float) -> None:
        """Called each cycle so AutoResearch setting changes take effect immediately."""
        self._min_edge = min_edge
        self._min_confidence = min_confidence
        self._bias_correction = bias_correction

    async def update_bias_from_calibration(self) -> None:
        """Refresh bias correction from real calibration data every N estimates."""
        if self._calibration is None:
            return
        self._estimates_since_calibration += 1
        if self._estimates_since_calibration < self._CALIBRATION_UPDATE_INTERVAL:
            return
        self._estimates_since_calibration = 0
        try:
            recommended = await self._calibration.get_recommended_bias_correction()
            if abs(recommended - self._bias_correction) > 0.005:
                log.info(
                    "bias_correction_updated",
                    old=self._bias_correction,
                    new=recommended,
                )
                self._bias_correction = recommended
        except Exception:
            log.exception("bias_calibration_error")

    async def estimate(
        self,
        question: str,
        market_price: float,
        end_date: str,
        news_items: list[NewsItem],
        lookback_hours: int = 2,
    ) -> EnsembleEstimate | None:
        # Auto-update bias correction from calibration data
        await self.update_bias_from_calibration()

        # Stage 1: Primary model
        try:
            primary = await self._llm.estimate_probability(
                question=question,
                market_price=market_price,
                end_date=end_date,
                news_items=news_items,
                provider=self._primary_provider,
                model=self._primary_model,
                lookback_hours=lookback_hours,
            )
        except LLMError:
            log.exception("primary_llm_error", question=question[:50])
            return None

        # Apply bias correction to primary estimate
        corrected_primary = self._apply_bias_correction(primary.probability, market_price)

        # Check if there's a potential edge worth confirming
        edge = abs(corrected_primary - market_price)
        if edge < self._min_edge:
            log.info(
                "no_edge_stage1",
                question=question[:50],
                market=market_price,
                estimate=round(corrected_primary, 3),
                raw=round(primary.probability, 3),
                edge=round(edge, 3),
                min_edge=self._min_edge,
            )
            return None

        if primary.confidence < self._min_confidence:
            log.info(
                "low_confidence_stage1",
                question=question[:50],
                confidence=round(primary.confidence, 3),
                min_confidence=self._min_confidence,
            )
            return None

        estimates = [primary]
        models_used = [primary.model_used]

        # Stage 2: Confirmation model — only if edge detected and ensemble enabled
        if self._use_ensemble:
            try:
                secondary = await self._llm.estimate_probability(
                    question=question,
                    market_price=market_price,
                    end_date=end_date,
                    news_items=news_items,
                    provider=self._secondary_provider,
                    model=self._secondary_model,
                    lookback_hours=lookback_hours,
                )
                estimates.append(secondary)
                models_used.append(secondary.model_used)

                corrected_secondary = self._apply_bias_correction(
                    secondary.probability, market_price
                )

                primary_above = corrected_primary > market_price
                secondary_above = corrected_secondary > market_price

                if primary_above != secondary_above:
                    # Models disagree on direction — heavy confidence penalty, but don't
                    # throw away entirely. The primary model might be right.
                    log.info(
                        "ensemble_disagreement",
                        question=question[:50],
                        primary=corrected_primary,
                        secondary=corrected_secondary,
                        market=market_price,
                    )
                    # Use primary estimate but slash confidence by 60%
                    final_prob = corrected_primary
                    final_confidence = primary.confidence * 0.40
                else:
                    # Models agree on direction — confidence-weighted average
                    w1 = primary.confidence
                    w2 = secondary.confidence
                    total_w = w1 + w2
                    if total_w > 0:
                        final_prob = (corrected_primary * w1 + corrected_secondary * w2) / total_w
                    else:
                        final_prob = (corrected_primary + corrected_secondary) / 2

                    # Geometric mean of confidences — punishes low confidence harder than
                    # arithmetic mean, but less harsh than min(). Floor at 0.01 to prevent
                    # a single zero-confidence model from zeroing the ensemble.
                    final_confidence = max(0.01, (w1 * w2) ** 0.5)

            except LLMError:
                log.warning("secondary_llm_error", msg="proceeding with primary only")
                final_prob = corrected_primary
                final_confidence = primary.confidence * 0.80
        else:
            final_prob = corrected_primary
            final_confidence = primary.confidence

        # Sanity check: if our estimate is extremely far from market, apply extra skepticism.
        # Markets are efficient enough that >30pp divergence is almost always noise.
        divergence = abs(final_prob - market_price)
        if divergence > 0.30:
            log.info(
                "extreme_divergence_dampened",
                question=question[:50],
                divergence=divergence,
                original_prob=final_prob,
            )
            # Dampen: pull 40% back toward market price
            final_prob = final_prob * 0.60 + market_price * 0.40
            final_confidence *= 0.70

        # Final edge check after ensemble + dampening
        final_edge = abs(final_prob - market_price)
        if final_edge < self._min_edge:
            log.debug("no_edge_final", edge=final_edge)
            return None

        if final_confidence < self._min_confidence:
            log.debug("low_confidence_final", confidence=final_confidence)
            return None

        # Score news freshness (how much of the news is <6h old)
        news_freshness = self._score_news_freshness(news_items)

        # Combine reasoning
        all_factors = []
        all_counters = []
        for est in estimates:
            all_factors.extend(est.key_factors)
            all_counters.extend(est.counter_arguments)

        result = EnsembleEstimate(
            probability=final_prob,
            confidence=final_confidence,
            reasoning=primary.reasoning,
            key_factors=list(dict.fromkeys(all_factors)),
            counter_arguments=list(dict.fromkeys(all_counters)),
            models_used=models_used,
            raw_estimates=estimates,
            news_freshness_score=news_freshness,
        )

        log.info(
            "estimate_complete",
            question=question[:50],
            probability=final_prob,
            confidence=final_confidence,
            market_price=market_price,
            edge=final_edge,
            models=models_used,
            news_freshness=news_freshness,
        )

        return result

    @staticmethod
    def _platt_scale(prob: float, alpha: float = 1.0) -> float:
        """Optional Platt scaling hook.

        The current production path keeps `alpha=1.0` (no extremizing). The
        previous >1.0 setting made already-confident estimates even more
        extreme before bias correction, which pushed outputs away from the
        market and broke downstream edge/confidence logic.
        """
        if alpha == 1.0:
            return prob
        # Log-odds extremizing (better than linear for probabilities)
        if prob <= 0.01 or prob >= 0.99:
            return prob
        odds = prob / (1 - prob)
        new_odds = odds ** alpha
        return max(0.01, min(0.99, new_odds / (1 + new_odds)))

    def _apply_bias_correction(self, estimate: float, market_price: float) -> float:
        """Two-stage calibration: Platt scaling + proportional bias correction.

        1. Platt scaling: extremize LLM estimate away from 0.5 (fixes hedging)
        2. Proportional correction: pull toward market price (fixes overconfidence)
        """
        # Stage 1: Platt scaling — push away from 0.5
        estimate = self._platt_scale(estimate)

        divergence = estimate - market_price
        abs_div = abs(divergence)

        if abs_div < 0.001:
            return estimate

        # Stage 2: Proportional bias correction
        correction_fraction = min(self._bias_correction * 3, 0.50)
        correction_amount = abs_div * correction_fraction
        correction_amount = min(correction_amount, abs_div * 0.80)

        if divergence > 0:
            corrected = estimate - correction_amount
        else:
            corrected = estimate + correction_amount

        return max(0.01, min(0.99, corrected))

    @staticmethod
    def _score_news_freshness(news_items: list[NewsItem]) -> float:
        """Score 0-1 based on how fresh the news is. Fresh news = higher edge likelihood."""
        if not news_items:
            return 0.0

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        fresh_count = 0
        for item in news_items[:10]:
            try:
                pub = item.published_at
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
                age_hours = (now - pub).total_seconds() / 3600
                if age_hours < 6:
                    fresh_count += 1
            except (AttributeError, TypeError):
                continue

        return fresh_count / min(len(news_items), 10)
