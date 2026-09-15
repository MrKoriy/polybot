"""Level 3: Meta-strategy — analyzes trade history to find what works.

Discovers which market categories, time windows, and conditions produce
the best results, then auto-adjusts filters and preferences.
"""

from dataclasses import dataclass
from datetime import datetime

import structlog

from analysis.market_filter import MarketFilter, classify_market_question
from config.settings import Settings
from research.experiment_log import ExperimentLog, ExperimentResult
from storage.repository import TradeRepository

log = structlog.get_logger()


@dataclass
class CategoryStats:
    category: str
    trades: int
    wins: int
    losses: int
    total_pnl: float
    avg_edge: float
    win_rate: float


@dataclass
class MetaInsight:
    insight_type: str  # "prefer_category", "avoid_category", "adjust_hours", "adjust_volume"
    description: str
    confidence: float  # 0-1, how confident are we in this insight
    action: dict  # What to change


class MetaStrategy:
    """Analyzes trade history to find patterns and auto-adjust strategy."""

    def __init__(
        self,
        settings: Settings,
        trade_repo: TradeRepository,
        market_filter: MarketFilter,
        experiment_log: ExperimentLog,
    ) -> None:
        self._settings = settings
        self._trade_repo = trade_repo
        self._market_filter = market_filter
        self._log = experiment_log
        self._experiment_count = 0
        self._preferred_categories: set[str] = set()
        self._avoided_categories: set[str] = set()
        self._insights_history: list[MetaInsight] = []

    async def analyze_and_adapt(self) -> list[MetaInsight]:
        """Analyze all trades and generate actionable insights."""
        self._experiment_count += 1
        insights: list[MetaInsight] = []

        trades = await self._trade_repo.get_recent_trades(limit=200)
        if len(trades) < 5:
            log.info("meta_skip", reason="not enough trades", count=len(trades))
            return insights

        # 1. Category analysis
        category_insights = self._analyze_categories(trades)
        insights.extend(category_insights)

        # 2. Time-of-day analysis
        time_insights = self._analyze_time_patterns(trades)
        insights.extend(time_insights)

        # 3. Edge size analysis — which edge ranges are profitable?
        edge_insights = self._analyze_edge_ranges(trades)
        insights.extend(edge_insights)

        # 4. Position size analysis
        size_insights = self._analyze_position_sizes(trades)
        insights.extend(size_insights)

        # Apply insights — lowered threshold from 0.6 to 0.4 so more patterns are acted on
        applied = 0
        for insight in insights:
            if insight.confidence >= 0.4:
                self._apply_insight(insight)
                applied += 1

        # Log meta experiment
        result = ExperimentResult(
            experiment_id=f"M{self._experiment_count:04d}",
            level="meta",
            description=f"Meta-analysis: {len(insights)} insights, {applied} applied",
            status="keep" if applied > 0 else "discard",
            pnl=0.0,
            trades_count=len(trades),
            cycles_run=0,
            started_at=datetime.utcnow().isoformat(),
            finished_at=datetime.utcnow().isoformat(),
        )
        self._log.log_result(result)

        self._insights_history.extend(insights)
        return insights

    def _analyze_categories(self, trades) -> list[MetaInsight]:
        """Find which market categories are profitable."""
        insights = []
        categories: dict[str, CategoryStats] = {}

        for t in trades:
            # Extract category from question keywords
            cat = self._classify_trade(t)
            if cat not in categories:
                categories[cat] = CategoryStats(
                    category=cat, trades=0, wins=0, losses=0,
                    total_pnl=0.0, avg_edge=0.0, win_rate=0.0,
                )
            s = categories[cat]
            s.trades += 1
            s.total_pnl += t.pnl
            s.avg_edge += t.edge
            if t.pnl > 0:
                s.wins += 1
            elif t.pnl < 0:
                s.losses += 1

        for cat, stats in categories.items():
            if stats.trades < 3:
                continue
            stats.win_rate = stats.wins / stats.trades
            stats.avg_edge /= stats.trades

            if stats.win_rate >= 0.6 and stats.total_pnl > 0:
                insights.append(MetaInsight(
                    insight_type="prefer_category",
                    description=f"Category '{cat}' is profitable: {stats.win_rate:.0%} win rate, ${stats.total_pnl:.2f} P&L over {stats.trades} trades",
                    confidence=min(0.9, 0.5 + stats.trades * 0.05),
                    action={"prefer": cat},
                ))
            elif stats.win_rate < 0.4 and stats.total_pnl < 0:
                insights.append(MetaInsight(
                    insight_type="avoid_category",
                    description=f"Category '{cat}' is unprofitable: {stats.win_rate:.0%} win rate, ${stats.total_pnl:.2f} P&L over {stats.trades} trades",
                    confidence=min(0.9, 0.5 + stats.trades * 0.05),
                    action={"avoid": cat},
                ))

        return insights

    def _analyze_time_patterns(self, trades) -> list[MetaInsight]:
        """Find which hours of day are most profitable for trading."""
        insights = []
        hourly: dict[int, dict] = {}

        for t in trades:
            if not t.created_at:
                continue
            hour = t.created_at.hour
            if hour not in hourly:
                hourly[hour] = {"trades": 0, "pnl": 0.0, "wins": 0}
            hourly[hour]["trades"] += 1
            hourly[hour]["pnl"] += t.pnl
            if t.pnl > 0:
                hourly[hour]["wins"] += 1

        # Find best and worst hours
        profitable_hours = []
        unprofitable_hours = []

        for hour, data in hourly.items():
            if data["trades"] < 2:
                continue
            win_rate = data["wins"] / data["trades"]
            if win_rate >= 0.6 and data["pnl"] > 0:
                profitable_hours.append(hour)
            elif win_rate < 0.3 and data["pnl"] < 0:
                unprofitable_hours.append(hour)

        if profitable_hours:
            # Scale confidence with data volume: more trades across profitable hours = higher confidence
            total_trades_in_hours = sum(hourly[h]["trades"] for h in profitable_hours)
            confidence = min(0.75, 0.4 + total_trades_in_hours * 0.03)
            insights.append(MetaInsight(
                insight_type="best_hours",
                description=f"Most profitable hours (UTC): {sorted(profitable_hours)}",
                confidence=confidence,
                action={"best_hours": sorted(profitable_hours)},
            ))

        return insights

    def _analyze_edge_ranges(self, trades) -> list[MetaInsight]:
        """Find which edge sizes lead to profitable trades."""
        insights = []
        edge_buckets: dict[str, dict] = {
            "tiny_2-4%": {"min": 0.02, "max": 0.04, "trades": 0, "pnl": 0.0, "wins": 0},
            "small_4-6%": {"min": 0.04, "max": 0.06, "trades": 0, "pnl": 0.0, "wins": 0},
            "medium_6-10%": {"min": 0.06, "max": 0.10, "trades": 0, "pnl": 0.0, "wins": 0},
            "large_10%+": {"min": 0.10, "max": 1.0, "trades": 0, "pnl": 0.0, "wins": 0},
        }

        for t in trades:
            for bucket_name, bucket in edge_buckets.items():
                if bucket["min"] <= abs(t.edge) < bucket["max"]:
                    bucket["trades"] += 1
                    bucket["pnl"] += t.pnl
                    if t.pnl > 0:
                        bucket["wins"] += 1
                    break

        best_bucket = None
        best_pnl_per_trade = float("-inf")

        for bucket_name, bucket in edge_buckets.items():
            if bucket["trades"] < 3:
                continue
            pnl_per_trade = bucket["pnl"] / bucket["trades"]
            if pnl_per_trade > best_pnl_per_trade:
                best_pnl_per_trade = pnl_per_trade
                best_bucket = bucket_name

        if best_bucket:
            b = edge_buckets[best_bucket]
            win_rate = b["wins"] / b["trades"]
            insights.append(MetaInsight(
                insight_type="best_edge_range",
                description=f"Best edge range: {best_bucket} ({win_rate:.0%} win rate, ${b['pnl']:.2f} total P&L)",
                confidence=min(0.8, 0.4 + b["trades"] * 0.05),
                action={"optimal_min_edge": b["min"]},
            ))

        # Warn if tiny edges are losing money
        tiny = edge_buckets["tiny_2-4%"]
        if tiny["trades"] >= 3 and tiny["pnl"] < 0:
            win_rate = tiny["wins"] / tiny["trades"]
            insights.append(MetaInsight(
                insight_type="raise_min_edge",
                description=f"Tiny edges (2-4%) are unprofitable: {win_rate:.0%} win rate, ${tiny['pnl']:.2f} P&L. Consider raising min_edge.",
                confidence=min(0.85, 0.5 + tiny["trades"] * 0.05),
                action={"raise_min_edge_to": 0.04},
            ))

        return insights

    def _analyze_position_sizes(self, trades) -> list[MetaInsight]:
        """Check if position sizing is optimal."""
        insights = []

        if len(trades) < 10:
            return insights

        # Compare P&L of small vs large positions
        median_size = sorted(t.size for t in trades)[len(trades) // 2]
        small = [t for t in trades if t.size <= median_size]
        large = [t for t in trades if t.size > median_size]

        if small and large:
            small_pnl = sum(t.pnl for t in small) / len(small)
            large_pnl = sum(t.pnl for t in large) / len(large)

            if small_pnl > large_pnl * 1.5 and small_pnl > 0:
                insights.append(MetaInsight(
                    insight_type="reduce_position_size",
                    description=f"Smaller positions more profitable (avg ${small_pnl:.3f}) than larger (avg ${large_pnl:.3f}). Consider reducing max_position_pct.",
                    confidence=0.6,
                    action={"reduce_max_position_pct": True},
                ))

        return insights

    def _apply_insight(self, insight: MetaInsight) -> None:
        """Apply a meta-insight to live settings."""
        log.info("meta_insight_applied", type=insight.insight_type, description=insight.description)

        if insight.insight_type == "prefer_category":
            self._preferred_categories.add(insight.action["prefer"])
            self._market_filter.set_preferred_categories(self._preferred_categories)

        elif insight.insight_type == "avoid_category":
            self._avoided_categories.add(insight.action["avoid"])
            self._market_filter.set_avoided_categories(self._avoided_categories)

        elif insight.insight_type == "raise_min_edge":
            new_edge = insight.action.get("raise_min_edge_to", 0.04)
            if new_edge > self._settings.min_edge:
                old = self._settings.min_edge
                self._settings.min_edge = new_edge
                log.info("meta_adjusted_min_edge", old=old, new=new_edge)

        elif insight.insight_type == "best_edge_range":
            optimal = insight.action.get("optimal_min_edge")
            if optimal and abs(optimal - self._settings.min_edge) > 0.01:
                old = self._settings.min_edge
                self._settings.min_edge = optimal
                log.info("meta_adjusted_min_edge", old=old, new=optimal)

        elif insight.insight_type == "reduce_position_size":
            if self._settings.max_position_pct > 0.10:
                old = self._settings.max_position_pct
                self._settings.max_position_pct = max(0.08, old - 0.10)
                log.info("meta_adjusted_position_size", old=old, new=self._settings.max_position_pct)

    def _classify_trade(self, trade) -> str:
        """Classify a trade into a category based on its question."""
        return classify_market_question(trade.question)

    @property
    def preferred_categories(self) -> set[str]:
        return self._preferred_categories

    @property
    def avoided_categories(self) -> set[str]:
        return self._avoided_categories

    def get_insights_report(self) -> str:
        """Format insights for Telegram/logging."""
        if not self._insights_history:
            return "No meta-insights yet. Need more trades."

        lines = ["<b>Meta-Strategy Insights</b>\n"]
        for i, insight in enumerate(self._insights_history[-10:], 1):
            confidence_bar = "●" * int(insight.confidence * 5) + "○" * (5 - int(insight.confidence * 5))
            lines.append(
                f"{i}. [{confidence_bar}] {insight.description}"
            )

        if self._preferred_categories:
            lines.append(f"\n✅ Preferred: {', '.join(self._preferred_categories)}")
        if self._avoided_categories:
            lines.append(f"❌ Avoided: {', '.join(self._avoided_categories)}")

        return "\n".join(lines)
