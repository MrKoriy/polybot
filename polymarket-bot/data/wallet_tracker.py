"""Polymarket wallet tracker — monitors top traders via public Data API.

Polls the activity endpoint for a list of tracked proxy wallet addresses.
No API key needed. Detects new trades and emits signals for the copy trader.
"""
import asyncio
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import structlog

log = structlog.get_logger()

DATA_API = "https://data-api.polymarket.com"

# Poll interval per wallet (seconds). With 10 wallets at 2s each = 20s full cycle.
POLL_INTERVAL_PER_WALLET = 2.0

# Minimum trade size for whale detection
WHALE_TRADE_MIN_SIZE = 100  # $100+

# How many recent market trades to scan for whale discovery
DISCOVERY_TRADE_LIMIT = 100


def _parse_timestamp(value) -> datetime:
    """Parse Data API timestamps, which may be Unix seconds or ISO-8601."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        epoch = float(value)
        if epoch > 10_000_000_000:  # tolerate millisecond timestamps
            epoch /= 1000.0
        return datetime.fromtimestamp(epoch, tz=timezone.utc)

    raw = str(value or "").strip()
    if raw:
        try:
            epoch = float(raw)
            if epoch > 10_000_000_000:
                epoch /= 1000.0
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


@dataclass
class WalletTrade:
    """A single trade detected from a tracked wallet."""
    wallet: str
    condition_id: str
    asset_id: str  # token_id
    market_slug: str
    title: str
    side: str  # "BUY" or "SELL"
    outcome: str  # "Yes" / "No" / "Up" / "Down"
    price: float
    size_usd: float
    timestamp: datetime
    tx_hash: str = ""


@dataclass
class WalletProfile:
    """Tracked wallet with metadata."""
    address: str
    label: str = ""
    specialty: str = ""  # "sports", "crypto", "politics", "general"
    trades_seen: int = 0
    last_trade_ts: float = 0.0  # monotonic timestamp of last detected trade


class WalletTracker:
    """Monitors a list of wallets and yields new trades."""

    def __init__(self, wallets: list[WalletProfile]) -> None:
        self._wallets = {w.address.lower(): w for w in wallets}
        self._http = httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PolyBot/1.0)"},
        )
        # Track last seen trade per wallet to detect new ones
        self._last_seen_tx: dict[str, str] = {}  # wallet -> last tx_hash
        self._running = False

    @property
    def wallet_count(self) -> int:
        return len(self._wallets)

    async def poll_all(self) -> list[WalletTrade]:
        """Poll all tracked wallets, return new trades since last poll."""
        all_new: list[WalletTrade] = []

        for addr, profile in self._wallets.items():
            try:
                new_trades = await self._poll_wallet(addr, profile)
                all_new.extend(new_trades)
            except Exception:
                log.debug("wallet_poll_error", wallet=addr[:10])

            # Rate limit between wallets
            await asyncio.sleep(POLL_INTERVAL_PER_WALLET)

        return all_new

    async def _poll_wallet(self, address: str, profile: WalletProfile) -> list[WalletTrade]:
        """Poll a single wallet for new trades."""
        resp = await self._http.get(
            f"{DATA_API}/activity",
            params={
                "user": address,
                "type": "TRADE",
                "sortDirection": "DESC",
                "limit": 10,
            },
        )

        if resp.status_code != 200:
            return []

        activities = resp.json()
        if not activities:
            return []

        # Find new trades (ones we haven't seen before)
        last_tx = self._last_seen_tx.get(address, "")
        new_trades: list[WalletTrade] = []

        for act in activities:
            tx = act.get("transactionHash", "") or act.get("id", "")

            # Stop when we reach already-seen trades
            if tx == last_tx:
                break

            # Parse trade
            trade = self._parse_activity(address, act)
            if trade:
                new_trades.append(trade)

        # Update last seen
        if activities:
            first_tx = activities[0].get("transactionHash", "") or activities[0].get("id", "")
            if first_tx:
                self._last_seen_tx[address] = first_tx

        if new_trades:
            profile.trades_seen += len(new_trades)
            profile.last_trade_ts = _time.monotonic()
            log.info(
                "wallet_new_trades",
                wallet=address[:10],
                label=profile.label,
                count=len(new_trades),
            )

        return new_trades

    @staticmethod
    def _parse_activity(wallet: str, act: dict) -> WalletTrade | None:
        """Parse a Data API activity record into a WalletTrade."""
        try:
            # The activity endpoint returns varied formats
            condition_id = act.get("conditionId", "") or act.get("market", "")
            asset_id = act.get("assetId", "") or act.get("asset", "")
            title = act.get("title", "") or act.get("question", "")
            slug = act.get("slug", "") or act.get("marketSlug", "")
            side = (act.get("side", "") or act.get("type", "")).upper()
            outcome = act.get("outcome", "") or act.get("outcomeName", "")
            price = float(act.get("price", 0) or 0)
            raw_usdc_size = act.get("usdcSize")
            if raw_usdc_size not in (None, ""):
                size_usd = float(raw_usdc_size or 0)
            else:
                size_tokens = float(act.get("size", 0) or 0)
                size_usd = size_tokens * price
            tx = act.get("transactionHash", "") or act.get("id", "")

            # Parse timestamp
            timestamp = (
                act.get("timestamp")
                or act.get("createdAt")
                or act.get("timestamp_epoch")
                or act.get("blockTimestamp")
            )
            ts = _parse_timestamp(timestamp)

            if not condition_id or not side or size_usd < 1.0:
                return None

            # Normalize side
            if side in ("BUY", "TRADE", "MARKET_BUY"):
                side = "BUY"
            elif side in ("SELL", "MARKET_SELL"):
                side = "SELL"
            else:
                return None

            return WalletTrade(
                wallet=wallet,
                condition_id=condition_id,
                asset_id=asset_id,
                market_slug=slug,
                title=title,
                side=side,
                outcome=outcome,
                price=price,
                size_usd=size_usd,
                timestamp=ts,
                tx_hash=tx,
            )
        except Exception:
            return None

    async def discover_whales(self) -> list[WalletTrade]:
        """Scan recent large trades across all markets to find whale activity.

        Uses the public trades endpoint — no specific wallet needed.
        Returns trades from wallets making $100+ trades.
        """
        try:
            resp = await self._http.get(
                f"{DATA_API}/trades",
                params={
                    "limit": DISCOVERY_TRADE_LIMIT,
                    "takerOnly": "false",
                },
            )
            if resp.status_code != 200:
                return []

            raw_trades = resp.json()
            if not isinstance(raw_trades, list):
                return []

            whale_trades: list[WalletTrade] = []
            for t in raw_trades:
                wallet = t.get("proxyWallet", "")
                if not wallet:
                    continue
                side = (t.get("side", "") or "").upper()
                if side not in ("BUY", "SELL"):
                    continue

                condition_id = t.get("conditionId", "")
                asset_id = t.get("asset", "")
                title = t.get("title", "")
                slug = t.get("slug", "")
                price = float(t.get("price", 0) or 0)
                size_tokens = float(t.get("size", 0) or 0)
                size_usd = size_tokens * price
                if size_usd < WHALE_TRADE_MIN_SIZE:
                    continue

                ts = _parse_timestamp(t.get("timestamp") or t.get("createdAt"))

                # Determine outcome from asset position (first token = Yes/Up)
                outcome = t.get("outcome", "")

                whale_trades.append(WalletTrade(
                    wallet=wallet.lower(),
                    condition_id=condition_id,
                    asset_id=asset_id,
                    market_slug=slug,
                    title=title,
                    side=side,
                    outcome=outcome,
                    price=price,
                    size_usd=size_usd,
                    timestamp=ts,
                ))

            if whale_trades:
                log.info("whales_discovered", count=len(whale_trades))

            return whale_trades

        except Exception:
            log.debug("whale_discovery_error")
            return []

    def add_wallet(self, address: str, label: str = "", specialty: str = "") -> None:
        """Dynamically add a wallet to track."""
        addr = address.lower()
        if addr not in self._wallets:
            self._wallets[addr] = WalletProfile(
                address=addr, label=label or addr[:10], specialty=specialty,
            )

    async def close(self) -> None:
        await self._http.aclose()
