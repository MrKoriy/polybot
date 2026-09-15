"""Experiment logging — like autoresearch's results.tsv but for trading strategies."""

import csv
import fcntl
import os
from dataclasses import dataclass, field
from datetime import datetime

import structlog

log = structlog.get_logger()


@dataclass
class ExperimentResult:
    experiment_id: str
    level: str  # "params", "prompt", "meta"
    description: str
    status: str  # "keep", "discard", "crash"
    # Metrics
    pnl: float = 0.0
    win_rate: float = 0.0
    brier_score: float = 0.0
    trades_count: int = 0
    cycles_run: int = 0
    # Params snapshot
    params_snapshot: dict = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""


class ExperimentLog:
    """TSV logger for autoresearch experiments."""

    def __init__(self, path: str = "data/experiments.tsv") -> None:
        self._path = path
        self._ensure_file()

    def _ensure_file(self) -> None:
        if not os.path.exists(self._path):
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", newline="") as f:
                writer = csv.writer(f, delimiter="\t")
                writer.writerow([
                    "experiment_id", "level", "status", "pnl", "win_rate",
                    "brier_score", "trades", "cycles", "description",
                    "started_at", "finished_at",
                ])

    def log_result(self, result: ExperimentResult) -> None:
        self._ensure_file()  # Re-check in case file was deleted
        with open(self._path, "a", newline="") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                writer = csv.writer(f, delimiter="\t")
                writer.writerow([
                    result.experiment_id,
                    result.level,
                    result.status,
                    f"{result.pnl:.4f}",
                    f"{result.win_rate:.4f}",
                    f"{result.brier_score:.4f}",
                    result.trades_count,
                    result.cycles_run,
                    result.description,
                    result.started_at,
                    result.finished_at,
                ])
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        log.info(
            "experiment_logged",
            id=result.experiment_id,
            level=result.level,
            status=result.status,
            pnl=result.pnl,
        )

    def get_best_result(self, level: str) -> ExperimentResult | None:
        """Get the best keep'd experiment for a given level."""
        if not os.path.exists(self._path):
            return None

        best = None
        best_pnl = float("-inf")

        with open(self._path, newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                if row["level"] == level and row["status"] == "keep":
                    pnl = float(row["pnl"])
                    if pnl > best_pnl:
                        best_pnl = pnl
                        best = ExperimentResult(
                            experiment_id=row["experiment_id"],
                            level=level,
                            description=row["description"],
                            status="keep",
                            pnl=pnl,
                            win_rate=float(row["win_rate"]),
                            brier_score=float(row["brier_score"]),
                            trades_count=int(row["trades"]),
                            cycles_run=int(row["cycles"]),
                            started_at=row["started_at"],
                            finished_at=row["finished_at"],
                        )
        return best

    def get_all_results(self, level: str | None = None) -> list[ExperimentResult]:
        if not os.path.exists(self._path):
            return []

        results = []
        with open(self._path, newline="") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                if level and row["level"] != level:
                    continue
                results.append(ExperimentResult(
                    experiment_id=row["experiment_id"],
                    level=row["level"],
                    description=row["description"],
                    status=row["status"],
                    pnl=float(row["pnl"]),
                    win_rate=float(row["win_rate"]),
                    brier_score=float(row["brier_score"]),
                    trades_count=int(row["trades"]),
                    cycles_run=int(row["cycles"]),
                    started_at=row["started_at"],
                    finished_at=row["finished_at"],
                ))
        return results
