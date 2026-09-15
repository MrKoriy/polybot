from dataclasses import dataclass

import structlog

from analysis.market_filter import classify_market_question
from storage.repository import PredictionRepository

log = structlog.get_logger()


@dataclass
class CalibrationStats:
    total_predictions: int
    resolved_predictions: int
    brier_score: float
    win_rate: float
    avg_confidence: float
    accuracy_by_bucket: dict[str, dict]  # e.g. "60-70": {"count": 10, "correct": 7}
    bias_estimate: float  # positive = overconfident, negative = underconfident
    brier_by_category: dict[str, float] = None  # e.g. {"politics": 0.12, "sports": 0.18}

    def __post_init__(self):
        if self.brier_by_category is None:
            self.brier_by_category = {}


class CalibrationTracker:
    def __init__(self, prediction_repo: PredictionRepository) -> None:
        self._repo = prediction_repo

    async def record_prediction(
        self,
        market_id: str,
        question: str,
        estimated_prob: float,
        market_price: float,
        confidence: float,
        model_used: str,
        reasoning: str = "",
    ) -> int:
        pred = await self._repo.create_prediction(
            market_id=market_id,
            question=question,
            estimated_prob=estimated_prob,
            market_price=market_price,
            confidence=confidence,
            model_used=model_used,
            reasoning=reasoning,
        )
        return pred.id

    async def resolve_prediction(self, prediction_id: int, actual_outcome: float) -> None:
        await self._repo.resolve_prediction(prediction_id, actual_outcome)

    async def resolve_by_market_id(self, market_id: str, actual_outcome: float) -> int:
        """Resolve all unresolved predictions for a market when it resolves.

        Returns number of predictions resolved.
        """
        predictions = await self._repo.get_unresolved_for_market(market_id)
        for pred in predictions:
            await self._repo.resolve_prediction(pred.id, actual_outcome)
        if predictions:
            log.info(
                "predictions_resolved",
                market_id=market_id,
                count=len(predictions),
                outcome=actual_outcome,
            )
        return len(predictions)

    async def get_stats(self, limit: int = 100) -> CalibrationStats:
        resolved = await self._repo.get_resolved_predictions(limit=limit)
        unresolved = await self._repo.get_unresolved_predictions()

        total = len(resolved) + len(unresolved)

        if not resolved:
            return CalibrationStats(
                total_predictions=total,
                resolved_predictions=0,
                brier_score=0.0,
                win_rate=0.0,
                avg_confidence=0.0,
                accuracy_by_bucket={},
                bias_estimate=0.0,
            )

        # Brier score: mean of (estimate - outcome)^2
        brier_sum = 0.0
        wins = 0
        confidence_sum = 0.0
        buckets: dict[str, dict] = {}
        bias_sum = 0.0

        for pred in resolved:
            outcome = pred.actual_outcome
            estimate = pred.estimated_prob

            # Brier score
            brier_sum += (estimate - outcome) ** 2

            # Win rate (did we predict the right direction?)
            predicted_yes = estimate > 0.5
            actual_yes = outcome > 0.5
            if predicted_yes == actual_yes:
                wins += 1

            confidence_sum += pred.confidence

            # Bias: positive means we predicted too high on average
            bias_sum += estimate - outcome

            # Bucket by 10% ranges
            bucket_key = f"{int(estimate * 10) * 10}-{int(estimate * 10) * 10 + 10}"
            if bucket_key not in buckets:
                buckets[bucket_key] = {"count": 0, "correct": 0, "avg_estimate": 0.0}
            buckets[bucket_key]["count"] += 1
            if predicted_yes == actual_yes:
                buckets[bucket_key]["correct"] += 1
            buckets[bucket_key]["avg_estimate"] += estimate

        n = len(resolved)
        for bucket in buckets.values():
            bucket["avg_estimate"] /= bucket["count"]
            bucket["accuracy"] = bucket["correct"] / bucket["count"]

        # Per-category Brier score (inferred from question text)
        cat_sums: dict[str, list[float]] = {}
        for pred in resolved:
            cat = classify_market_question(pred.question)
            cat_sums.setdefault(cat, [])
            cat_sums[cat].append((pred.estimated_prob - pred.actual_outcome) ** 2)
        brier_by_category = {cat: sum(vals) / len(vals) for cat, vals in cat_sums.items()}

        return CalibrationStats(
            total_predictions=total,
            resolved_predictions=n,
            brier_score=brier_sum / n,
            win_rate=wins / n,
            avg_confidence=confidence_sum / n,
            accuracy_by_bucket=buckets,
            bias_estimate=bias_sum / n,
            brier_by_category=brier_by_category,
        )

    async def get_recommended_bias_correction(self) -> float:
        stats = await self.get_stats()
        if stats.resolved_predictions < 10:
            return 0.04  # Default before enough data

        # If bias_estimate is positive, we're overconfident -> increase correction
        # If negative, we're underconfident -> decrease correction
        return max(0.0, min(0.10, 0.04 + stats.bias_estimate))

    async def format_report(self) -> str:
        stats = await self.get_stats()

        lines = [
            "<b>Calibration Report</b>",
            "",
            f"Total predictions: {stats.total_predictions}",
            f"Resolved: {stats.resolved_predictions}",
            f"Brier score: {stats.brier_score:.4f} (lower is better, <0.25 is good)",
            f"Win rate: {stats.win_rate*100:.1f}%",
            f"Avg confidence: {stats.avg_confidence*100:.1f}%",
            f"Bias estimate: {stats.bias_estimate:+.3f}",
            "",
            "<b>Accuracy by bucket:</b>",
        ]

        for bucket, data in sorted(stats.accuracy_by_bucket.items()):
            lines.append(
                f"  {bucket}%: {data['correct']}/{data['count']} "
                f"({data.get('accuracy', 0)*100:.0f}%)"
            )

        if stats.brier_by_category:
            lines.extend(["", "<b>Brier by category:</b>"])
            for cat, brier in sorted(stats.brier_by_category.items()):
                rating = "good" if brier < 0.20 else "ok" if brier < 0.25 else "poor"
                lines.append(f"  {cat}: {brier:.4f} ({rating})")

        return "\n".join(lines)
