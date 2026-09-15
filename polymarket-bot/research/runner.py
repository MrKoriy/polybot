"""AutoResearch Runner — the infinite experiment loop.

Like Karpathy's autoresearch: NEVER STOP. Run experiments while the human sleeps.
Cycles through all three levels:
  Level 1 (params):  every experiment window
  Level 2 (prompts): every 3rd experiment window
  Level 3 (meta):    every 5th experiment window
"""

import asyncio
from datetime import datetime

import structlog

from analysis.calibration_tracker import CalibrationTracker
from analysis.market_filter import MarketFilter
from config.settings import Settings
from research.autotuner import ParameterAutotuner
from research.experiment_log import ExperimentLog
from research.meta_strategy import MetaStrategy
from research.prompt_optimizer import PromptOptimizer
from storage.repository import TradeRepository
from trading.portfolio import Portfolio

log = structlog.get_logger()


class AutoResearchRunner:
    """Autonomous research loop. Runs forever, optimizing the bot while you sleep."""

    def __init__(
        self,
        settings: Settings,
        portfolio: Portfolio,
        calibration: CalibrationTracker,
        trade_repo: TradeRepository,
        market_filter: MarketFilter,
        cycles_per_experiment: int = 10,
    ) -> None:
        self._settings = settings
        self._portfolio = portfolio
        self._calibration = calibration
        self._trade_repo = trade_repo

        self._experiment_log = ExperimentLog(path="data/experiments.tsv")

        self._autotuner = ParameterAutotuner(
            settings=settings,
            experiment_log=self._experiment_log,
            cycles_per_experiment=cycles_per_experiment,
        )

        self._prompt_optimizer = PromptOptimizer(
            experiment_log=self._experiment_log,
            cycles_per_experiment=cycles_per_experiment,
        )

        self._meta_strategy = MetaStrategy(
            settings=settings,
            trade_repo=trade_repo,
            market_filter=market_filter,
            experiment_log=self._experiment_log,
        )

        self._cycle_count = 0
        self._experiment_window = 0
        self._cycles_per_experiment = cycles_per_experiment
        self._running = False

        # Current active level
        self._active_level: str | None = None  # "params", "prompt", "meta"
        self._window_start_cycle = 0

    async def on_cycle_complete(self, cycle_count: int) -> None:
        """Called after each trading pipeline cycle. Manages experiment lifecycle."""
        self._cycle_count = cycle_count

        if not self._running:
            return

        # Start a new experiment window if none active
        if self._active_level is None:
            await self._start_next_experiment()
            return

        # Check if current experiment window is complete
        cycles_in_window = cycle_count - self._window_start_cycle
        if cycles_in_window >= self._cycles_per_experiment:
            await self._finish_experiment()
            # Immediately start the next one — NEVER STOP
            await self._start_next_experiment()

    async def _start_next_experiment(self) -> None:
        """Decide which level to run and start the experiment."""
        self._experiment_window += 1
        self._window_start_cycle = self._cycle_count

        # Schedule: L1 every time, L2 every 3rd, L3 every 5th
        if self._experiment_window % 5 == 0:
            self._start_meta_analysis()
        elif self._experiment_window % 3 == 0:
            await self._start_prompt_experiment()
        else:
            self._start_param_experiment()

    def _start_param_experiment(self) -> None:
        """Level 1: Start a parameter tuning experiment."""
        experiment = self._autotuner.propose_experiment()
        if experiment is None:
            log.info("autoresearch_skip", level="params", reason="no valid experiment")
            self._active_level = None
            return

        self._autotuner.apply_experiment(experiment)
        self._autotuner.start_measurement(
            bankroll=self._portfolio.bankroll,
            total_trades=self._portfolio._total_trades,
            wins=self._portfolio._wins,
            cycle=self._cycle_count,
        )
        self._active_level = "params"
        log.info(
            "autoresearch_start",
            level="params",
            window=self._experiment_window,
            description=experiment.description,
        )

    async def _start_prompt_experiment(self) -> None:
        """Level 2: Start a prompt optimization experiment."""
        experiment = self._prompt_optimizer.propose_experiment()
        if experiment is None:
            self._start_param_experiment()  # Fallback
            return

        self._prompt_optimizer.apply_experiment(experiment)

        # Fetch real baseline brier score for comparison at experiment end
        stats = await self._calibration.get_stats()
        brier = stats.brier_score
        self._prompt_optimizer.start_measurement(
            bankroll=self._portfolio.bankroll,
            total_trades=self._portfolio._total_trades,
            wins=self._portfolio._wins,
            brier=brier,
            cycle=self._cycle_count,
        )
        self._active_level = "prompt"
        log.info(
            "autoresearch_start",
            level="prompt",
            window=self._experiment_window,
            description=experiment.description,
        )

    def _start_meta_analysis(self) -> None:
        """Level 3: Run meta-strategy analysis (single-shot, not a window)."""
        self._active_level = "meta"
        log.info("autoresearch_start", level="meta", window=self._experiment_window)

    async def _finish_experiment(self) -> None:
        """Evaluate and keep/discard the current experiment."""
        if self._active_level == "params":
            stats = await self._calibration.get_stats()
            result = self._autotuner.evaluate_experiment(
                bankroll=self._portfolio.bankroll,
                total_trades=self._portfolio._total_trades,
                wins=self._portfolio._wins,
                cycle=self._cycle_count,
                brier_score=stats.brier_score,
            )
            log.info(
                "autoresearch_result",
                level="params",
                status=result.status,
                pnl=result.pnl,
                trades=result.trades_count,
            )

        elif self._active_level == "prompt":
            stats = await self._calibration.get_stats()
            result = self._prompt_optimizer.evaluate_experiment(
                bankroll=self._portfolio.bankroll,
                total_trades=self._portfolio._total_trades,
                wins=self._portfolio._wins,
                brier_score=stats.brier_score,
                cycle=self._cycle_count,
            )
            log.info(
                "autoresearch_result",
                level="prompt",
                status=result.status,
                brier=result.brier_score,
                pnl=result.pnl,
            )

        elif self._active_level == "meta":
            insights = await self._meta_strategy.analyze_and_adapt()
            log.info(
                "autoresearch_result",
                level="meta",
                insights=len(insights),
                applied=[i.description for i in insights if i.confidence >= 0.4],
            )

        self._active_level = None

    def start(self) -> None:
        """Enable autoresearch. Called once at bot startup."""
        self._running = True
        log.info(
            "autoresearch_enabled",
            cycles_per_experiment=self._cycles_per_experiment,
            schedule="L1 every window, L2 every 3rd, L3 every 5th",
        )

    def stop(self) -> None:
        """Disable autoresearch."""
        self._running = False
        # Revert any active experiment
        if self._active_level == "params" and self._autotuner.has_active_experiment:
            self._autotuner.revert_experiment()
        elif self._active_level == "prompt" and self._prompt_optimizer.has_active_experiment:
            self._prompt_optimizer.revert_experiment()
        self._active_level = None
        log.info("autoresearch_disabled")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def active_level(self) -> str | None:
        return self._active_level

    @property
    def experiment_window(self) -> int:
        return self._experiment_window

    @property
    def meta_strategy(self) -> MetaStrategy:
        return self._meta_strategy

    @property
    def experiment_log(self) -> ExperimentLog:
        return self._experiment_log

    def status_report(self) -> str:
        """Format status for Telegram/dashboard."""
        all_results = self._experiment_log.get_all_results()
        kept = [r for r in all_results if r.status == "keep"]
        discarded = [r for r in all_results if r.status == "discard"]

        lines = [
            "🔬 <b>AutoResearch Status</b>",
            "",
            f"Running: {'✅' if self._running else '❌'}",
            f"Experiment window: #{self._experiment_window}",
            f"Active level: {self._active_level or 'idle'}",
            f"Total experiments: {len(all_results)}",
            f"Kept: {len(kept)} | Discarded: {len(discarded)}",
        ]

        # Last 5 experiments
        if all_results:
            lines.append("\n<b>Recent experiments:</b>")
            for r in all_results[-5:]:
                emoji = "✅" if r.status == "keep" else "❌"
                lines.append(
                    f"  {emoji} [{r.level}] {r.description[:50]} "
                    f"(P&L: ${r.pnl:+.3f}, trades: {r.trades_count})"
                )

        # Best params found
        best_params = self._autotuner.best_params
        if best_params:
            lines.append("\n<b>Current best params:</b>")
            for k, v in best_params.items():
                lines.append(f"  {k}: {v}")

        # Meta insights
        meta_report = self._meta_strategy.get_insights_report()
        if "No meta-insights" not in meta_report:
            lines.append(f"\n{meta_report}")

        return "\n".join(lines)
