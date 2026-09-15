"""Copy trading module — tracks top wallets, uses basket consensus to trade.

Runs independently on its own cycle (every 30s). Does NOT touch the main
trading pipeline, estimator, or BTC trader.

Strategy:
1. Poll 10-15 tracked wallets via Data API
2. Aggregate: when 2+ wallets enter the SAME market on the SAME side → signal
3. Filter: skip markets we already hold, check liquidity, apply risk limits
4. Execute: paper trade (or live) with size proportional to consensus strength
"""
import asyncio
import time as _time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import structlog

from data.wallet_tracker import WalletProfile, WalletTrade, WalletTracker
from storage.repository import TradeRepository
from trading.portfolio import Portfolio

log = structlog.get_logger()

GAMMA_API = "https://gamma-api.polymarket.com"

# --- Configuration ---
CYCLE_INTERVAL = 30  # seconds

# Require 3+ wallets consensus (was 2 — too noisy)
MIN_CONSENSUS = 3

CONSENSUS_WINDOW = 3600  # 1 hour

# Cap position size — avg loss was $122 vs avg win $53, so reduce exposure
MAX_COPY_POSITION_PCT = 0.04  # 4% max (was 10%)
MAX_COPY_BET = 50.0  # Hard cap $50 per copy trade

MIN_BET = 5.0
MIN_TIME_TO_RESOLUTION = 600  # 10 minutes
MAX_COPY_POSITIONS = 2  # Max 2 simultaneous (was 3)
MIN_WHALE_TRADE_SIZE = 50.0  # $50 minimum whale trade (was $10 — too noisy)

# Skip copy trades with entry price > 0.75 (downside too large relative to upside)
MAX_COPY_ENTRY_PRICE = 0.75


# Seed wallets — verified profitable traders from Polymarket leaderboard.
# Copy trader also auto-discovers new whales from large trades.
SEED_WALLETS = [
    # reachingthesky — $25K→$3.7M, European soccer, event-driven sports
    WalletProfile(address="0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1", label="reachingthesky", specialty="sports"),
    # LucasMeow — $243K PnL, 94.9% win rate, systematic approach
    WalletProfile(address="0x7f3c8979d0afa00007bae4747d5347122af05613", label="LucasMeow", specialty="general"),
    # Tsybka — $191K PnL, 85.9% win rate, consistent since 2024
    WalletProfile(address="0xd5ccdf772f795547e299de57f47966e24de8dea4", label="Tsybka", specialty="general"),
    # RN1 — $1K→$2M, 13K+ trades, sports arbitrage
    WalletProfile(address="0x2005d16a84ceefa912d4e380cd32e7ff827875ea", label="RN1", specialty="sports"),
    # Anon high-PnL — $2.6M profit, 63% WR, concentrated directional
    WalletProfile(address="0x9d84ce0306f8551e02efef1680475fc0f1dc1344", label="high_pnl_anon", specialty="general"),
    # Anon consistent — $958K profit, 65-67% WR
    WalletProfile(address="0xd218e474776403a330142299f7796e8ba32eb5c9", label="consistent_anon", specialty="general"),
    # High volume — $1.34M PnL, $1.4M 7d volume
    WalletProfile(address="0xee613b3fc183ee44f9da9c05f53e2da107e3debf", label="high_vol", specialty="general"),
    # Theo4 — $20M+ profit, macro political conviction (less copyable but good signal)
    WalletProfile(address="0x56687bf447db6ffa42ffe2204a05edaa20f55839", label="Theo4", specialty="politics"),
]


@dataclass
class CopySignal:
    """A consensus-based copy trading signal."""
    condition_id: str
    title: str
    side: str  # "BUY"
    outcome: str  # "Yes" / "No"
    avg_price: float
    total_size: float  # combined USD size from all wallets
    wallets: list[str]  # wallet labels that agree
    wallet_count: int
    consensus_strength: float  # wallet_count / total_tracked
    first_seen: datetime
    asset_id: str = ""


class CopyTrader:
    """Wallet basket copy trader — runs independently of main pipeline."""

    def __init__(
        self,
        portfolio: Portfolio,
        wallets: list[WalletProfile] | None = None,
        paper_mode: bool = True,
        trade_repo: TradeRepository | None = None,
    ) -> None:
        self._portfolio = portfolio
        self._paper_mode = paper_mode
        self._trade_repo = trade_repo
        # Start with seed wallets (verified profitable) + auto-discover new whales
        self._tracker = WalletTracker(wallets or SEED_WALLETS)
        self._http = httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PolyBot/1.0)"},
        )
        self._running = False
        self._cycle_count = 0
        self._trades_count = 0

        # Accumulator: condition_id -> list of WalletTrades within consensus window
        self._trade_buffer: dict[str, list[WalletTrade]] = defaultdict(list)

        # Track which markets we've already copy-traded to avoid duplicates
        self._copied_markets: set[str] = set()

        # Known whale addresses discovered from large trades
        self._known_whales: set[str] = set()

    async def start(self) -> None:
        self._running = True
        log.info("copy_trader_started", wallets=self._tracker.wallet_count)
        asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        await self._tracker.close()
        await self._http.aclose()
        log.info("copy_trader_stopped", trades=self._trades_count)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._run_cycle()
            except Exception:
                log.exception("copy_trader_cycle_error")
            await asyncio.sleep(CYCLE_INTERVAL)

    async def _run_cycle(self) -> None:
        self._cycle_count += 1

        # 1. Discover whales from recent large trades (primary method)
        whale_trades = await self._tracker.discover_whales()

        # Auto-register new whales for future tracking
        for t in whale_trades:
            if t.wallet not in self._known_whales and t.size_usd >= MIN_WHALE_TRADE_SIZE:
                self._known_whales.add(t.wallet)
                self._tracker.add_wallet(t.wallet, label=f"whale_{len(self._known_whales)}")

        # 2. Also poll known wallets for their latest trades
        tracked_trades = await self._tracker.poll_all()

        # 3. Combine and filter — only buys of $10+
        all_trades = whale_trades + tracked_trades
        all_trades = [t for t in all_trades if t.side == "BUY" and t.size_usd >= MIN_WHALE_TRADE_SIZE]

        if all_trades:
            log.info("copy_whale_activity", discovered=len(whale_trades), tracked=len(tracked_trades))

        # 4. Add to buffer
        for trade in all_trades:
            self._trade_buffer[trade.condition_id].append(trade)

        # 5. Clean old trades from buffer
        self._clean_buffer()

        # 6. Check for consensus signals
        signals = self._find_consensus_signals()

        # 7. Execute signals
        for signal in signals:
            await self._execute_signal(signal)

    def _clean_buffer(self) -> None:
        """Remove trades older than consensus window."""
        now = datetime.now(timezone.utc)
        for cid in list(self._trade_buffer.keys()):
            trades = self._trade_buffer[cid]
            fresh = [t for t in trades if (now - t.timestamp).total_seconds() < CONSENSUS_WINDOW]
            if fresh:
                self._trade_buffer[cid] = fresh
            else:
                del self._trade_buffer[cid]

    def _find_consensus_signals(self) -> list[CopySignal]:
        """Find markets where 2+ tracked wallets agree."""
        signals = []

        for cid, trades in self._trade_buffer.items():
            # Skip if already copied
            if cid in self._copied_markets:
                continue

            # Group by outcome (Yes/No/Up/Down)
            by_outcome: dict[str, list[WalletTrade]] = defaultdict(list)
            for t in trades:
                # Deduplicate: one trade per wallet per outcome
                wallets_in_outcome = [x.wallet for x in by_outcome[t.outcome]]
                if t.wallet not in wallets_in_outcome:
                    by_outcome[t.outcome].append(t)

            # Check each outcome for consensus
            for outcome, outcome_trades in by_outcome.items():
                unique_wallets = set(t.wallet for t in outcome_trades)
                if len(unique_wallets) >= MIN_CONSENSUS:
                    avg_price = sum(t.price for t in outcome_trades) / len(outcome_trades)
                    total_size = sum(t.size_usd for t in outcome_trades)
                    wallet_labels = []
                    for t in outcome_trades:
                        addr = t.wallet.lower()
                        profile = self._tracker._wallets.get(addr)
                        label = profile.label if profile else addr[:10]
                        wallet_labels.append(label)

                    signals.append(CopySignal(
                        condition_id=cid,
                        title=outcome_trades[0].title,
                        side="BUY",
                        outcome=outcome,
                        avg_price=avg_price,
                        total_size=total_size,
                        wallets=wallet_labels,
                        wallet_count=len(unique_wallets),
                        consensus_strength=len(unique_wallets) / self._tracker.wallet_count,
                        first_seen=min(t.timestamp for t in outcome_trades),
                        asset_id=outcome_trades[0].asset_id,
                    ))

        return signals

    async def _execute_signal(self, signal: CopySignal) -> None:
        """Execute a consensus copy trade signal."""
        # Check limits
        copy_positions = sum(
            1 for p in self._portfolio.open_positions
            if "COPY" in (p.question or "").upper()
        )
        if copy_positions >= MAX_COPY_POSITIONS:
            log.debug("copy_max_positions", count=copy_positions)
            return

        # Check if already holding this market
        if signal.condition_id in self._portfolio.held_market_ids:
            return

        # Size the bet based on consensus strength
        bankroll = self._portfolio.bankroll
        if bankroll <= 0:
            return

        # Stronger consensus = bigger bet (2 wallets = 5%, 3+ = up to 10%)
        fraction = min(
            MAX_COPY_POSITION_PCT,
            0.03 + signal.consensus_strength * 0.15,
        )
        bet_size = round(min(bankroll * fraction, MAX_COPY_BET), 2)
        if bet_size < MIN_BET:
            return

        # Validate price — skip expensive entries where downside > upside
        price = signal.avg_price
        if price <= 0.02 or price >= 0.98:
            return
        if price > MAX_COPY_ENTRY_PRICE:
            log.debug("copy_price_too_high", price=price, max=MAX_COPY_ENTRY_PRICE)
            return

        # Determine token_id — fetch from Gamma if not in signal
        token_id = signal.asset_id
        if not token_id:
            token_id = await self._get_token_id(signal.condition_id, signal.outcome)
            if not token_id:
                log.debug("copy_no_token", market=signal.title[:40])
                return

        log.info(
            "copy_trade_signal",
            market=signal.title[:50],
            outcome=signal.outcome,
            wallets=signal.wallets,
            consensus=signal.wallet_count,
            strength=round(signal.consensus_strength, 2),
            avg_price=round(price, 3),
            whale_size=round(signal.total_size, 0),
            bet_size=bet_size,
        )

        # Execute
        direction = f"BUY_{signal.outcome.upper()}"
        copy_question = f"COPY {signal.wallet_count}w | {signal.title[:40]}"
        copy_trade_id = 0
        if self._trade_repo:
            import time as _time
            copy_trade = await self._trade_repo.create_trade(
                market_id=signal.condition_id, token_id=token_id,
                question=copy_question, side="BUY", direction=direction,
                price=price, size=bet_size, edge=0.0, kelly_fraction=0.0,
                estimated_prob=0.0, market_price_at_entry=price,
                confidence=signal.consensus_strength, status="filled",
                paper_mode=self._paper_mode,
                order_id=f"copy_{int(_time.time())}",
                reasoning=f"Consensus: {', '.join(signal.wallets)}",
            )
            copy_trade_id = copy_trade.id

        self._portfolio.open_position(
            trade_id=copy_trade_id,
            market_id=signal.condition_id,
            token_id=token_id,
            question=copy_question,
            direction=direction,
            price=price,
            size=bet_size,
        )
        self._copied_markets.add(signal.condition_id)
        self._trades_count += 1

        log.info(
            "copy_paper_trade",
            market=signal.title[:50],
            outcome=signal.outcome,
            direction=direction,
            price=price,
            size=bet_size,
            wallets=signal.wallets,
            trade_id=copy_trade_id,
        )

    async def _get_token_id(self, condition_id: str, outcome: str) -> str | None:
        """Fetch token_id for a market outcome from Gamma API."""
        try:
            resp = await self._http.get(
                f"{GAMMA_API}/markets",
                params={"conditionId": condition_id, "limit": 1},
            )
            if resp.status_code != 200:
                return None

            markets = resp.json()
            if not markets:
                return None

            market = markets[0]
            if market.get("conditionId") != condition_id:
                return None

            import json
            raw_tokens = market.get("clobTokenIds", "[]")
            tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
            outcomes = market.get("outcomes", [])

            if not tokens or not outcomes:
                return None

            # Match outcome name to token
            outcome_lower = outcome.lower()
            for i, name in enumerate(outcomes):
                if name.lower() == outcome_lower and i < len(tokens):
                    return str(tokens[i])

            # Fallback: Yes=0, No=1
            if outcome_lower in ("yes", "up") and tokens:
                return str(tokens[0])
            elif outcome_lower in ("no", "down") and len(tokens) > 1:
                return str(tokens[1])

            return str(tokens[0]) if tokens else None

        except Exception:
            return None

    def status(self) -> dict:
        return {
            "running": self._running,
            "cycle_count": self._cycle_count,
            "trades": self._trades_count,
            "tracked_wallets": self._tracker.wallet_count,
            "buffer_markets": len(self._trade_buffer),
            "copied_markets": len(self._copied_markets),
        }
