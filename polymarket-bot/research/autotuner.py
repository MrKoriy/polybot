"""Level 1: Autonomous parameter tuning — like autoresearch modifies hyperparameters.

Each experiment tweaks ONE parameter, runs N trading cycles, measures P&L/win_rate,
then keeps or discards the change.
"""

import random
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

import structlog

from config.settings import Settings
from research.experiment_log import ExperimentLog, ExperimentResult

log = structlog.get_logger()

# Parameter search space: (param_name, min, max, step, type)
PARAM_SPACE = [
    ("min_edge", 0.02, 0.12, 0.01, float),
    ("min_confidence", 0.45, 0.85, 0.05, float),
    ("kelly_fraction", 0.10, 0.50, 0.05, float),
    ("llm_bias_correction", 0.00, 0.12, 0.02, float),
    ("max_hours_to_resolution", 6, 96, 6, int),
    ("max_position_pct", 0.10, 0.40, 0.05, float),
    ("news_lookback_hours", 4, 48, 4, int),
    ("top_markets_to_analyze", 10, 40, 5, int),
]


@dataclass
class ParamExperiment:
    param_name: str
    old_value: float | int
    new_value: float | int
    description: str


class ParameterAutotuner:
    """Systematically tests parameter variations, keeps improvements."""

    def __init__(
        self,
        settings: Settings,
        experiment_log: ExperimentLog,
        cycles_per_experiment: int = 10,
    ) -> None:
        self._settings = settings
        self._log = experiment_log
        self._cycles_per_experiment = cycles_per_experiment
        self._experiment_count = 0
        self._current_experiment: ParamExperiment | None = None

        # Baseline snapshot
        self._baseline_params = self._snapshot_params()
        self._best_params = deepcopy(self._baseline_params)

        # Metrics tracking for current experiment
        self._experiment_start_bankroll = 0.0
        self._experiment_start_trades = 0
        self._experiment_start_wins = 0
        self._experiment_start_cycle = 0

    def _snapshot_params(self) -> dict:
        return {name: getattr(self._settings, name) for name, *_ in PARAM_SPACE}

    def propose_experiment(self) -> ParamExperiment | None:
        """Propose a new parameter change to test."""
        self._experiment_count += 1

        # Pick a random parameter to tweak
        param_name, p_min, p_max, step, p_type = random.choice(PARAM_SPACE)
        current_value = getattr(self._settings, param_name)

        # Choose direction: up or down (weighted by unexplored territory)
        if random.random() < 0.5:
            new_value = current_value + step
        else:
            new_value = current_value - step

        # Clamp to bounds
        new_value = max(p_min, min(p_max, new_value))
        new_value = p_type(round(new_value, 4) if p_type == float else new_value)

        # Skip if same as current
        if new_value == current_value:
            new_value = current_value + step if current_value - step < p_min else current_value - step
            new_value = max(p_min, min(p_max, new_value))
            new_value = p_type(round(new_value, 4) if p_type == float else new_value)

        if new_value == current_value:
            return None

        experiment = ParamExperiment(
            param_name=param_name,
            old_value=current_value,
            new_value=new_value,
            description=f"{param_name}: {current_value} -> {new_value}",
        )

        log.info(
            "param_experiment_proposed",
            param=param_name,
            old=current_value,
            new=new_value,
            experiment_num=self._experiment_count,
        )

        return experiment

    def apply_experiment(self, experiment: ParamExperiment) -> None:
        """Apply parameter change to live settings."""
        self._current_experiment = experiment
        setattr(self._settings, experiment.param_name, experiment.new_value)
        log.info("param_applied", param=experiment.param_name, value=experiment.new_value)

    def revert_experiment(self) -> None:
        """Revert to previous parameter value."""
        if self._current_experiment:
            setattr(
                self._settings,
                self._current_experiment.param_name,
                self._current_experiment.old_value,
            )
            log.info(
                "param_reverted",
                param=self._current_experiment.param_name,
                value=self._current_experiment.old_value,
            )
            self._current_experiment = None

    def start_measurement(self, bankroll: float, total_trades: int, wins: int, cycle: int) -> None:
        """Snapshot metrics at experiment start."""
        self._experiment_start_bankroll = bankroll
        self._experiment_start_trades = total_trades
        self._experiment_start_wins = wins
        self._experiment_start_cycle = cycle

    def evaluate_experiment(
        self,
        bankroll: float,
        total_trades: int,
        wins: int,
        cycle: int,
        brier_score: float = 0.0,
    ) -> ExperimentResult:
        """Evaluate if the experiment improved things."""
        if not self._current_experiment:
            raise ValueError("No active experiment to evaluate")

        pnl = bankroll - self._experiment_start_bankroll
        new_trades = total_trades - self._experiment_start_trades
        new_wins = wins - self._experiment_start_wins
        win_rate = new_wins / new_trades if new_trades > 0 else 0.0
        cycles_run = cycle - self._experiment_start_cycle

        # Decision: keep if P&L positive OR (no trades but no harm)
        # With few trades, we're more lenient
        if new_trades == 0:
            status = "discard"  # No signal = discard
        elif pnl > 0:
            status = "keep"
        elif pnl == 0 and win_rate >= 0.5:
            status = "keep"  # Neutral but winning
        else:
            status = "discard"

        exp_id = f"P{self._experiment_count:04d}"
        result = ExperimentResult(
            experiment_id=exp_id,
            level="params",
            description=self._current_experiment.description,
            status=status,
            pnl=pnl,
            win_rate=win_rate,
            brier_score=brier_score,
            trades_count=new_trades,
            cycles_run=cycles_run,
            params_snapshot=self._snapshot_params(),
            started_at=datetime.utcnow().isoformat(),
            finished_at=datetime.utcnow().isoformat(),
        )

        self._log.log_result(result)

        if status == "keep":
            self._best_params = self._snapshot_params()
            log.info("param_experiment_kept", **{self._current_experiment.param_name: self._current_experiment.new_value, "pnl": pnl})
        else:
            self.revert_experiment()
            log.info("param_experiment_discarded", pnl=pnl, trades=new_trades)

        self._current_experiment = None
        return result

    @property
    def cycles_per_experiment(self) -> int:
        return self._cycles_per_experiment

    @property
    def has_active_experiment(self) -> bool:
        return self._current_experiment is not None

    @property
    def best_params(self) -> dict:
        return deepcopy(self._best_params)
