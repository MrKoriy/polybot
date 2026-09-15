"""Live-mode balance reconciliation.

In live mode we must NEVER let in-memory Portfolio._bankroll drift from the
real on-chain USDC balance. Every N seconds this background task:

  1. Reads real USDC balance from CLOB (via get_balance_allowance).
  2. Compares to Portfolio.bankroll.
  3. If mismatch > DRIFT_THRESHOLD: Telegram alert + force-resync internal state.

IMPORTANT: real_USDC must equal internal_bankroll (both represent FREE cash).
Position cost basis (locked) is held as CTF outcome tokens ON CHAIN, NOT as USDC
in the wallet. A previous version incorrectly compared `real_USDC` to
`bankroll + locked`, which triggered false drift alerts every cycle and
force-synced bankroll down to a wrong value.

This catches: silent partial fills, external withdrawals, failed sells that
look successful in logs, and any case where our cash tracking diverges.

Paper mode: does nothing (paper bankroll is simulated).
"""
from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger()

DRIFT_THRESHOLD_USDC = 2.0          # alert on diff > $2
CHECK_INTERVAL_SECONDS = 300        # 5 min
STARTUP_DELAY_SECONDS = 60          # don't check first minute (bot still initialising)


class BalanceReconciler:
    def __init__(self, polymarket, portfolio, telegram=None,
                 interval: int = CHECK_INTERVAL_SECONDS,
                 drift_threshold: float = DRIFT_THRESHOLD_USDC,
                 risk_repo=None) -> None:
        self._polymarket = polymarket
        self._portfolio = portfolio
        self._telegram = telegram
        self._interval = interval
        self._drift_threshold = drift_threshold
        self._risk_repo = risk_repo
        self._running = False
        self._last_real_balance: float | None = None
        self._consecutive_drifts = 0

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._loop())
        log.info("reconciler_started", interval_s=self._interval,
                 drift_threshold=self._drift_threshold)

    async def stop(self) -> None:
        self._running = False
        log.info("reconciler_stopped")

    async def _loop(self) -> None:
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        while self._running:
            try:
                await self._check_once()
            except Exception:
                log.exception("reconciler_error")
            await asyncio.sleep(self._interval)

    async def _check_once(self) -> None:
        real_balance = await self._polymarket.get_balance()
        if real_balance is None or real_balance <= 0:
            log.warning("reconciler_no_real_balance", raw=real_balance)
            return

        internal_bankroll = self._portfolio.bankroll
        locked = sum(p.size for p in self._portfolio.open_positions)
        # CORRECT invariant: real_USDC == internal_bankroll.
        # Position cost basis (locked) is CTF tokens on-chain, NOT USDC;
        # it does NOT contribute to the USDC wallet balance.
        diff = real_balance - internal_bankroll

        log.info(
            "reconciler_checked",
            real_usdc=round(real_balance, 4),
            internal_bankroll=round(internal_bankroll, 4),
            locked_ctf=round(locked, 4),  # informational only
            diff=round(diff, 4),
        )

        if abs(diff) > self._drift_threshold:
            self._consecutive_drifts += 1
            msg = (
                f"BALANCE DRIFT detected #{self._consecutive_drifts}:\n"
                f"  real USDC (wallet):    ${real_balance:,.4f}\n"
                f"  internal bankroll:     ${internal_bankroll:,.4f}\n"
                f"  diff (real - internal):${diff:+.4f}\n"
                f"  locked in CTF (info):  ${locked:,.4f}\n"
                f"  attribution: fees/slippage/partial-fills vs accounting"
            )
            log.warning("balance_drift", diff=diff, real=real_balance,
                        internal=internal_bankroll, locked=locked,
                        count=self._consecutive_drifts)

            if self._telegram:
                try:
                    await self._telegram.send_risk_alert("balance_drift", msg)
                except Exception:
                    log.exception("reconciler_alert_send_failed")

            if self._risk_repo:
                try:
                    await self._risk_repo.create_event(
                        event_type="balance_drift",
                        details=(
                            f"real={real_balance:.4f} internal={internal_bankroll:.4f} "
                            f"diff={diff:+.4f} locked={locked:.4f}"
                        ),
                    )
                except Exception:
                    log.exception("reconciler_event_write_failed")

            # Snap bankroll to the real wallet balance, but BOOK the delta
            # into daily PnL instead of silently erasing it — the old
            # force-sync converted real losses (fees, slippage, partial
            # fills) into invisible drift, making live results look better
            # than they were.
            self._portfolio.apply_reconciliation_adjustment(diff)
            log.info("balance_force_resync", new_bankroll=round(real_balance, 4),
                     booked_to_daily_pnl=round(diff, 4))
        else:
            if self._consecutive_drifts > 0:
                log.info("balance_drift_cleared",
                         was_drifting_for=self._consecutive_drifts)
            self._consecutive_drifts = 0

        self._last_real_balance = real_balance
