"""Circuit breakers — auto-halt strategies on decay, drawdown, or anomalies.

Traders consult `is_halted(strategy)` before each trade cycle. On trip, the
breaker writes persistent state (survives restart), fires a Telegram alert,
and the trader silently skips the cycle.

Breakers split into two scopes:
  - "__global__" halt — kills ALL strategies (e.g. 30% drawdown, daily loss,
    emergency kill-switch file)
  - per-strategy halt — kills only one ("weather", "threshold", "spread") on
    strategy-specific decay signals (consecutive losses, trailing WR drop)

State is JSON at `data/breakers_state.json`. File format:
  {"weather": {"tripped": true, "reason": "...",
               "tripped_at": "2026-04-24T11:00:00+00:00",
               "auto_reset_at": "2026-04-25T11:00:00+00:00"}}
"""
from __future__ import annotations

import json
import os
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import structlog

log = structlog.get_logger()

GLOBAL_KEY = "__global__"
EMERGENCY_FLAG_FILE = "/tmp/polymarket_emergency_halt"


@dataclass
class BreakerState:
    tripped: bool = False
    reason: str = ""
    tripped_at: str | None = None          # ISO8601 UTC
    auto_reset_at: str | None = None       # ISO8601 UTC — None = manual reset only
    metric_snapshot: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BreakerState":
        return cls(
            tripped=bool(d.get("tripped", False)),
            reason=str(d.get("reason", "")),
            tripped_at=d.get("tripped_at"),
            auto_reset_at=d.get("auto_reset_at"),
            metric_snapshot=d.get("metric_snapshot") or {},
        )


class CircuitBreakerManager:
    # ---- thresholds (pilot $100) ----
    DRAWDOWN_PCT = 0.50                    # halt all below 50% of peak
    DAILY_LOSS_PCT = 0.35                  # halt after 35% of day-start
    CONSECUTIVE_LOSSES = 3                 # paranoid for pilot — was 5
    WR_TRAILING_DAYS = 14                  # lookback for WR watchdog
    WR_MIN_TRADES = 10                     # need at least N trades to judge WR
    WR_MIN_THRESHOLD = 0.40                # halt strategy if trailing WR < 40%
    AUTO_RESET_HOURS_SOFT = 12.0           # soft halts auto-reset after 12h
    AUTO_RESET_HOURS_HARD = None           # drawdown/kill-switch — manual only
    # Cap on COMMITTED (open, cost-basis) exposure as a fraction of total
    # portfolio value. Reactive checks only see closed trades; positions here
    # resolve hours later, so a bot can commit its whole risk budget before
    # the first loss lands. This gate trips on exposure, not realized loss.
    # 60% leaves headroom over the combined strategy caps (~45-50%) while
    # still catching all-in commitment before any resolution.
    OPEN_RISK_PCT = 0.60
    OPEN_RISK_RESET_HOURS = 1.0            # re-check after positions resolve

    STRATEGIES = ("weather", "threshold", "spread")

    def __init__(
        self,
        portfolio,
        trade_repo,
        telegram=None,
        state_file: str = "data/breakers_state.json",
        drawdown_pct: float | None = None,
        daily_loss_pct: float | None = None,
        strategies: Iterable[str] | None = None,
    ) -> None:
        self._portfolio = portfolio
        self._trade_repo = trade_repo
        self._telegram = telegram
        self._state_file = state_file
        # Peak of TOTAL portfolio value (cash + open exposure at cost basis).
        # Tracked separately because portfolio.peak_bankroll only tracks cash
        # and would wrongly trip drawdown when capital moves into open positions.
        # Set BEFORE _load_state() so a persisted peak survives restarts.
        self._peak_total_value: float = 0.0
        self._state: dict[str, BreakerState] = self._load_state()
        # None = no sweep yet. A 0.0 sentinel compared against time.monotonic()
        # silently skipped every sweep while host uptime was under 60s.
        self._last_check_ts: float | None = None
        self._drawdown_pct = (
            self.DRAWDOWN_PCT if drawdown_pct is None else float(drawdown_pct)
        )
        self._daily_loss_pct = (
            self.DAILY_LOSS_PCT if daily_loss_pct is None else float(daily_loss_pct)
        )
        self._strategies = tuple(strategies or self.STRATEGIES)

    # ---- state persistence ----

    PEAK_STATE_KEY = "__peak_total__"

    def _load_state(self) -> dict[str, BreakerState]:
        if not os.path.exists(self._state_file):
            return {}
        try:
            with open(self._state_file, "r") as f:
                data = json.load(f)
            peak_entry = data.pop(self.PEAK_STATE_KEY, None)
            if isinstance(peak_entry, dict):
                try:
                    self._peak_total_value = float(peak_entry.get("total_value", 0.0))
                except (TypeError, ValueError):
                    pass
            return {k: BreakerState.from_dict(v) for k, v in data.items()}
        except Exception as e:
            log.warning("breakers_state_load_error", err=str(e)[:80])
            return {}

    def _save_state(self) -> None:
        try:
            Path(self._state_file).parent.mkdir(parents=True, exist_ok=True)
            payload = {k: v.to_dict() for k, v in self._state.items()}
            # Persist the drawdown peak so a restart mid-drawdown doesn't
            # silently reset it and mask prior losses.
            payload[self.PEAK_STATE_KEY] = {"total_value": self._peak_total_value}
            with open(self._state_file, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            log.error("breakers_state_save_error", err=str(e)[:120])

    # ---- public API ----

    def is_halted(self, strategy: str) -> tuple[bool, str]:
        """Check if strategy is halted. Called by traders before each cycle."""
        # 1) global halts
        gs = self._state.get(GLOBAL_KEY)
        if gs and gs.tripped:
            if self._should_auto_reset(gs):
                self._reset_internal(GLOBAL_KEY, reason="auto_reset")
            else:
                return True, f"global: {gs.reason}"
        # 2) strategy-specific halts
        ss = self._state.get(strategy)
        if ss and ss.tripped:
            if self._should_auto_reset(ss):
                self._reset_internal(strategy, reason="auto_reset")
                return False, ""
            return True, f"{strategy}: {ss.reason}"
        return False, ""

    def is_any_halted(self) -> bool:
        return any(s.tripped for s in self._state.values())

    async def reset(self, key: str, reason: str = "manual") -> None:
        """Manually reset a tripped breaker (called via Telegram or admin)."""
        self._reset_internal(key, reason)
        if self._telegram is not None:
            await self._telegram.send_risk_alert(
                "breaker_reset",
                f"[{key}] reset ({reason})",
            )

    def _reset_internal(self, key: str, reason: str) -> None:
        if key in self._state:
            log.info("breaker_reset", key=key, reason=reason,
                     prior=self._state[key].reason)
            self._state[key] = BreakerState()
            self._save_state()

    async def _trip(
        self,
        key: str,
        reason: str,
        auto_reset_hours: float | None,
        metric: dict | None = None,
    ) -> None:
        existing = self._state.get(key)
        if existing and existing.tripped:
            # Already tripped — don't re-alert, only update metric
            if metric:
                existing.metric_snapshot = metric
                self._save_state()
            return

        now = datetime.now(timezone.utc)
        auto_reset = None
        if auto_reset_hours is not None:
            auto_reset = (now + timedelta(hours=auto_reset_hours)).isoformat()

        self._state[key] = BreakerState(
            tripped=True,
            reason=reason,
            tripped_at=now.isoformat(),
            auto_reset_at=auto_reset,
            metric_snapshot=metric or {},
        )
        self._save_state()
        log.error("breaker_tripped", key=key, reason=reason, auto_reset=auto_reset,
                  metric=metric)
        if self._telegram is not None:
            auto_msg = (
                f" (auto-reset in {auto_reset_hours}h)"
                if auto_reset_hours
                else " (manual reset only)"
            )
            await self._telegram.send_risk_alert(
                "circuit_breaker",
                f"[{key}] TRIPPED: {reason}{auto_msg}",
            )

    def _should_auto_reset(self, bs: BreakerState) -> bool:
        if not bs.auto_reset_at:
            return False
        try:
            t = datetime.fromisoformat(bs.auto_reset_at)
            return datetime.now(timezone.utc) >= t
        except Exception:
            return False

    # ---- checks ----

    async def check_all(self) -> None:
        """Run all checks. Called by scheduler every N minutes."""
        now = _time.monotonic()
        if self._last_check_ts is not None and now - self._last_check_ts < 60:
            return  # rate-limit: minimum 60s between full sweeps
        self._last_check_ts = now

        try:
            await self._check_kill_switch()
            await self._check_open_risk()
            await self._check_drawdown()
            await self._check_daily_loss()
            for strategy in self._strategies:
                await self._check_consecutive_losses(strategy)
                await self._check_wr_trailing(strategy)
        except Exception:
            log.exception("circuit_breaker_check_failed")

    async def _check_kill_switch(self) -> None:
        """Emergency halt file — user creates to instantly halt all."""
        if os.path.exists(EMERGENCY_FLAG_FILE):
            await self._trip(
                GLOBAL_KEY,
                reason=f"emergency_flag_file: {EMERGENCY_FLAG_FILE}",
                auto_reset_hours=None,  # manual-only
                metric={"file": EMERGENCY_FLAG_FILE},
            )

    async def _check_open_risk(self) -> None:
        """Halt new entries when COMMITTED exposure exceeds a fixed fraction
        of total portfolio value.

        All other checks are reactive: they evaluate closed trades only, and
        positions resolve hours or days after entry — a bot can commit its
        whole risk budget before the first loss lands (this is exactly how
        the -$20/-$100 live loss slipped past every breaker). This gate trips
        on exposure-at-cost, not realized loss, and self-resets an hour later
        so positions that resolved in the meantime free the budget.
        """
        try:
            bankroll = float(self._portfolio.bankroll or 0.0)
            open_risk = sum(
                float(getattr(p, "size", 0) or 0)
                for p in self._portfolio.open_positions
            )
        except Exception:
            return
        total_value = bankroll + open_risk
        if total_value <= 0 or open_risk <= 0:
            return
        risk_pct = open_risk / total_value
        if risk_pct >= self.OPEN_RISK_PCT:
            await self._trip(
                GLOBAL_KEY,
                reason=(
                    f"open risk ${open_risk:.2f} = {risk_pct*100:.0f}% of total "
                    f"value (cap {self.OPEN_RISK_PCT*100:.0f}%) — halting new entries"
                ),
                auto_reset_hours=self.OPEN_RISK_RESET_HOURS,
                metric={
                    "open_risk": open_risk,
                    "bankroll": bankroll,
                    "total_value": total_value,
                    "risk_pct": risk_pct,
                },
            )

    async def _check_drawdown(self) -> None:
        """Halt all strategies if TOTAL portfolio value drops >DRAWDOWN_PCT from peak.

        Uses cash + open-position cost basis (not just cash), so opening a
        position doesn't trigger false drawdown. True drawdown only fires when
        positions resolve at loss.
        """
        try:
            bankroll = float(self._portfolio.bankroll or 0.0)
            open_exposure = 0.0
            for position in self._portfolio.open_positions:
                size = float(getattr(position, "size", 0) or 0)
                unrealized = getattr(position, "unrealized_pnl", None)
                try:
                    mark_to_market = size + float(unrealized)
                except (TypeError, ValueError):
                    mark_to_market = size
                open_exposure += max(0.0, mark_to_market)
        except Exception:
            return
        current_value = bankroll + open_exposure
        if current_value > self._peak_total_value:
            self._peak_total_value = current_value
            self._save_state()  # persist peak so restarts can't mask drawdown
        if self._peak_total_value <= 0:
            return
        drawdown = (self._peak_total_value - current_value) / self._peak_total_value
        if drawdown >= self._drawdown_pct:
            await self._trip(
                GLOBAL_KEY,
                reason=f"total value drawdown {drawdown*100:.1f}% "
                       f"(peak ${self._peak_total_value:.2f} → ${current_value:.2f})",
                auto_reset_hours=None,  # manual — requires human review
                metric={"peak_total": self._peak_total_value, "current_total": current_value,
                        "bankroll": bankroll, "exposure": open_exposure,
                        "drawdown_pct": drawdown},
            )

    async def _check_daily_loss(self) -> None:
        """Halt all for 12h if daily realized PnL exceeds -15% of day-start bankroll."""
        try:
            daily_pnl = float(self._portfolio.daily_pnl or 0.0)
            start = float(self._portfolio._daily_start_bankroll or 0.0)
        except Exception:
            return
        if start <= 0:
            return
        pnl_pct = daily_pnl / start
        if pnl_pct <= -self._daily_loss_pct:
            await self._trip(
                GLOBAL_KEY,
                reason=f"daily loss {pnl_pct*100:.1f}% of ${start:.2f}",
                auto_reset_hours=self.AUTO_RESET_HOURS_SOFT,
                metric={"start": start, "pnl": daily_pnl, "pnl_pct": pnl_pct},
            )

    async def _check_consecutive_losses(self, strategy: str) -> None:
        """Halt strategy if last N closed trades are all losses."""
        trades = await self._recent_closed_for_strategy(strategy, limit=self.CONSECUTIVE_LOSSES)
        if len(trades) < self.CONSECUTIVE_LOSSES:
            return
        if all((t.pnl or 0) < 0 for t in trades):
            total_loss = sum((t.pnl or 0) for t in trades)
            await self._trip(
                strategy,
                reason=f"{self.CONSECUTIVE_LOSSES} consecutive losses "
                       f"(total ${total_loss:.2f})",
                auto_reset_hours=self.AUTO_RESET_HOURS_SOFT,
                metric={"losses": self.CONSECUTIVE_LOSSES, "total_pnl": total_loss},
            )

    async def _check_wr_trailing(self, strategy: str) -> None:
        """Halt strategy if trailing-14d WR drops below 40%."""
        trades = await self._closed_in_last_days(strategy, days=self.WR_TRAILING_DAYS)
        if len(trades) < self.WR_MIN_TRADES:
            return
        wins = sum(1 for t in trades if (t.pnl or 0) > 0)
        wr = wins / len(trades)
        if wr < self.WR_MIN_THRESHOLD:
            await self._trip(
                strategy,
                reason=f"trailing {self.WR_TRAILING_DAYS}d WR={wr*100:.0f}% "
                       f"(n={len(trades)}, need ≥{self.WR_MIN_THRESHOLD*100:.0f}%)",
                auto_reset_hours=self.AUTO_RESET_HOURS_SOFT,
                metric={"wr": wr, "n": len(trades), "wins": wins},
            )

    # ---- trade queries ----

    async def _recent_closed_for_strategy(self, strategy: str, limit: int) -> list:
        """Last N closed trades matching strategy prefix in question field.

        STRICTLY live trades (paper_mode=False). The previous paper-fallback
        meant a lucky paper history kept a losing live run unhalted — paper
        PnL comes from simulated oracle resolutions and must never gate live
        risk decisions.
        """
        if self._trade_repo is None:
            return []
        try:
            all_closed = await self._trade_repo.get_closed_trades(paper_mode=False)
        except Exception:
            return []
        filtered = [t for t in all_closed if self._matches_strategy(t, strategy)]
        filtered.sort(key=lambda t: getattr(t, "id", 0), reverse=True)
        return filtered[:limit]

    async def _closed_in_last_days(self, strategy: str, days: int) -> list:
        """All closed LIVE trades for strategy in last `days` days."""
        if self._trade_repo is None:
            return []
        try:
            all_closed = await self._trade_repo.get_closed_trades(paper_mode=False)
        except Exception:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filtered = []
        for t in all_closed:
            if not self._matches_strategy(t, strategy):
                continue
            closed_at = getattr(t, "closed_at", None) or getattr(t, "created_at", None)
            if closed_at is None:
                continue
            if not isinstance(closed_at, datetime):
                try:
                    closed_at = datetime.fromisoformat(str(closed_at))
                except Exception:
                    continue
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)
            if closed_at >= cutoff:
                filtered.append(t)
        return filtered

    @staticmethod
    def _matches_strategy(trade, strategy: str) -> bool:
        """Match trade to strategy by question prefix (WEATHER/THRESH/SPREAD)."""
        q = (getattr(trade, "question", "") or "").upper()
        if strategy == "weather":
            return q.startswith("WEATHER ")
        if strategy == "threshold":
            return q.startswith("THRESH ") or q.startswith("THRESHOLD ")
        if strategy == "spread":
            return q.startswith("SPREAD ")
        if strategy == "complete_set":
            return q.startswith("MAKER ") or q.startswith("COMPLETE_SET ")
        return False

    # ---- status/reporting ----

    def status(self) -> dict:
        """Snapshot for Telegram/dashboard reporting."""
        out = {}
        for key, bs in self._state.items():
            if not bs.tripped:
                continue
            out[key] = {
                "reason": bs.reason,
                "tripped_at": bs.tripped_at,
                "auto_reset_at": bs.auto_reset_at,
                "metric": bs.metric_snapshot,
            }
        return out
