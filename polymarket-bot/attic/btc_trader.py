"""Multi-crypto Up/Down trader — BTC, ETH, SOL on 5-min and 15-min markets.

Runs on its own 20-second cycle. For each crypto × timeframe combination:
- Fetches momentum from Binance (1m candles, RSI, volume)
- Finds the current active market on Polymarket
- If momentum signal is strong, bets Up or Down
- Auto-transitions to next market when current one ends
"""
import asyncio
import json
import time as _time
from datetime import datetime, timezone

import httpx
import structlog

from data.btc_price import BTCPriceFeed, BTCSnapshot
from storage.repository import TradeRepository
from trading.portfolio import Portfolio

log = structlog.get_logger()

GAMMA_API = "https://gamma-api.polymarket.com"

# --- Crypto assets to trade ---
CRYPTO_ASSETS = {
    "BTC": {
        "binance_symbol": "BTCUSDT",
        "slug_prefix_5m": "btc-updown-5m",
        "slug_prefix_15m": "btc-updown-15m",
    },
    "ETH": {
        "binance_symbol": "ETHUSDT",
        "slug_prefix_5m": "eth-updown-5m",
        "slug_prefix_15m": "eth-updown-15m",
    },
    "SOL": {
        "binance_symbol": "SOLUSDT",
        "slug_prefix_5m": "sol-updown-5m",
        "slug_prefix_15m": "sol-updown-15m",
    },
}

# --- Timeframe configs --- ORIGINAL VALUES
TIMEFRAME_CONFIGS = {
    "5m": {
        "interval_sec": 300,
        "cutoff_sec": 60,
        "min_momentum": 0.05,
        "min_signal": 0.12,
        "max_position_pct": 0.06,
    },
    "15m": {
        "interval_sec": 900,
        "cutoff_sec": 120,
        "min_momentum": 0.08,
        "min_signal": 0.15,
        "max_position_pct": 0.10,
    },
}

CHECK_INTERVAL = 20  # seconds
MIN_BET = 0.30
MIN_RSI_DIVERGENCE = 4

# --- RISK CAPS (only useful limits, no aggressive filtering) ---
MAX_CRYPTO_POSITIONS = 3
MAX_CRYPTO_BET = 200.0


class CryptoMarketInfo:
    """Represents a single crypto Up/Down market."""

    def __init__(self, data: dict, market_data: dict) -> None:
        self.slug = data.get("slug", "")
        self.title = data.get("title", "")
        self.condition_id = market_data.get("conditionId", "")
        self.end_date_str = market_data.get("endDate", "")
        self.resolved = market_data.get("resolved", False)

        raw_prices = market_data.get("outcomePrices", '["0.5","0.5"]')
        if isinstance(raw_prices, str):
            prices = json.loads(raw_prices)
        else:
            prices = raw_prices
        self.up_price = float(prices[0]) if prices else 0.5
        self.down_price = float(prices[1]) if len(prices) > 1 else 1.0 - self.up_price

        raw_tokens = market_data.get("clobTokenIds", "[]")
        if isinstance(raw_tokens, str):
            tokens = json.loads(raw_tokens)
        else:
            tokens = raw_tokens
        self.up_token_id = str(tokens[0]) if tokens else ""
        self.down_token_id = str(tokens[1]) if len(tokens) > 1 else ""

        self.volume = float(market_data.get("volumeNum", 0) or 0)

        try:
            self.end_date = datetime.fromisoformat(self.end_date_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            self.end_date = None

    @property
    def seconds_until_end(self) -> float:
        if not self.end_date:
            return 0
        return (self.end_date - datetime.now(timezone.utc)).total_seconds()

    @property
    def is_active(self) -> bool:
        return not self.resolved and self.seconds_until_end > 0


class BTC15MinTrader:
    """Multi-crypto momentum trader — BTC, ETH, SOL × 5m, 15m."""

    def __init__(
        self,
        portfolio: Portfolio,
        binance_feed=None,
        paper_mode: bool = True,
        max_position_pct: float = 0.10,
        trade_repo: TradeRepository | None = None,
    ) -> None:
        self._portfolio = portfolio
        self._paper_mode = paper_mode
        self._binance_ws = binance_feed  # For resolution price lookup
        self._trade_repo = trade_repo
        self._price_feed = BTCPriceFeed()
        self._http = httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PolyBot/1.0)"},
        )
        self._running = False
        self._cycle_count = 0
        self._trades_count = 0
        self._wins = 0

        # Track positions per (crypto, timeframe) to avoid double-betting
        self._positioned: dict[str, str] = {}  # "BTC_5m" -> condition_id
        # Track Binance entry price per market for resolution
        self._entry_prices: dict[str, tuple[str, float]] = {}  # condition_id -> (crypto, binance_price_at_entry)

    async def start(self) -> None:
        self._running = True
        cryptos = list(CRYPTO_ASSETS.keys())
        log.info("crypto_trader_started", assets=cryptos, timeframes=["5m", "15m"])
        asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        await self._price_feed.close()
        await self._http.aclose()
        log.info("crypto_trader_stopped", trades=self._trades_count, wins=self._wins)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._run_cycle()
            except Exception:
                log.exception("crypto_trader_cycle_error")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _run_cycle(self) -> None:
        self._cycle_count += 1

        # Close expired crypto positions first
        await self._close_expired_positions()

        # Trade each crypto × timeframe
        for crypto_name, crypto_cfg in CRYPTO_ASSETS.items():
            # Get momentum once per crypto (shared across 5m/15m)
            snapshot = await self._price_feed.get_snapshot(crypto_cfg["binance_symbol"])
            if not snapshot:
                continue

            for tf_name, tf_cfg in TIMEFRAME_CONFIGS.items():
                try:
                    slug_prefix = crypto_cfg[f"slug_prefix_{tf_name}"]
                    key = f"{crypto_name}_{tf_name}"
                    await self._trade_slot(key, slug_prefix, tf_cfg, snapshot, crypto_name)
                except Exception:
                    log.debug("crypto_tf_error", crypto=crypto_name, tf=tf_name)

    async def _close_expired_positions(self) -> None:
        """Close crypto positions using Binance price to determine winner."""
        for pos in list(self._portfolio.open_positions):
            q = pos.question or ""
            # Strict filter: only questions starting with "BTC ", "ETH ", "SOL "
            # AND containing "5m" or "15m" (avoids THRESH/SPREAD false-matches).
            is_crypto = any(q.startswith(f"{c} ") and (" 5m " in q or " 15m " in q)
                            for c in CRYPTO_ASSETS.keys())
            if not is_crypto:
                continue

            if not pos.opened_at:
                continue
            opened = pos.opened_at.replace(tzinfo=timezone.utc) if pos.opened_at.tzinfo is None else pos.opened_at
            age_min = (datetime.now(timezone.utc) - opened).total_seconds() / 60

            is_5m = "5m" in q
            max_age = 7.0 if is_5m else 17.0

            if age_min < max_age:
                continue

            # Determine winner using Binance price comparison
            exit_price = self._resolve_with_binance(pos.market_id, pos.direction)
            if exit_price is None:
                continue  # can't resolve — leave for next cycle (prevents fake 0.5 payout)

            pnl = await self._portfolio.close_position(pos.market_id, exit_price)
            if pnl > 0:
                self._wins += 1

            log.info(
                "crypto_position_closed",
                question=q[:50],
                exit_price=exit_price,
                pnl=round(pnl, 4),
                age_min=round(age_min, 1),
            )

    def _resolve_with_binance(self, market_id: str, direction: str) -> float | None:
        """Determine if we won or lost using Binance entry vs current price.

        Returns 1.0 (won), 0.0 (lost), or None (can't resolve — don't close).
        A 0.5 fallback is NEVER returned — it creates illusory paper PnL because
        close_position treats it as realised payout (2¢→50¢ = 25× fake gain).
        """
        entry_data = self._entry_prices.get(market_id)
        if not entry_data:
            return None  # no entry snapshot — skip close, try next cycle

        crypto_name, entry_binance_price = entry_data

        # Get current Binance price
        current_price = None
        if self._binance_ws:
            current_price = self._binance_ws.get_price(crypto_name)
        if not current_price:
            return None  # no Binance price — skip close

        # Did price go up or down since entry?
        price_went_up = current_price >= entry_binance_price

        # Did we bet Up or Down?
        bet_up = "UP" in direction.upper()

        # We win if our bet matches reality
        we_won = (bet_up and price_went_up) or (not bet_up and not price_went_up)

        # Clean up entry price record
        self._entry_prices.pop(market_id, None)

        log.info(
            "crypto_resolution",
            market_id=market_id[:20],
            crypto=crypto_name,
            entry_price=round(entry_binance_price, 2),
            current_price=round(current_price, 2),
            direction=direction,
            result="WIN" if we_won else "LOSS",
        )

        return 1.0 if we_won else 0.0

    async def _get_resolution_price(self, market_id: str, direction: str) -> float:
        """Get resolution price: 1.0 if our side won, 0.0 if lost, 0.5 if unknown."""
        try:
            resp = await self._http.get(
                f"{GAMMA_API}/markets",
                params={"conditionId": market_id, "limit": 1},
            )
            if resp.status_code != 200:
                return 0.5

            markets = resp.json()
            if not markets:
                return 0.5

            m = markets[0]
            if not m.get("resolved", False):
                return 0.5  # Not yet resolved

            # Check outcome
            raw_prices = m.get("outcomePrices", "")
            if isinstance(raw_prices, str):
                prices = json.loads(raw_prices)
            else:
                prices = raw_prices

            if not prices:
                return 0.5

            # prices[0] = Up final price (1.0 or 0.0), prices[1] = Down
            up_final = float(prices[0])

            if "UP" in direction.upper():
                return up_final  # 1.0 if Up won, 0.0 if Down won
            else:
                return 1.0 - up_final  # inverse for Down

        except Exception:
            return 0.5

    def _count_crypto_positions(self) -> tuple[int, float]:
        """Count open crypto positions and total capital locked.

        Strict filter: must start with 'BTC '/'ETH '/'SOL ' AND contain
        ' 5m ' or ' 15m ' — to avoid counting THRESH/SPREAD positions.
        """
        count = 0
        capital = 0.0
        for p in self._portfolio.open_positions:
            q = p.question or ""
            if any(q.startswith(f"{c} ") and (" 5m " in q or " 15m " in q)
                   for c in CRYPTO_ASSETS):
                count += 1
                capital += p.size
        return count, capital

    async def _trade_slot(
        self, key: str, slug_prefix: str, tf_cfg: dict,
        snapshot: BTCSnapshot, crypto_name: str,
    ) -> None:
        # RISK CAP: max simultaneous crypto positions
        crypto_count, _ = self._count_crypto_positions()
        if crypto_count >= MAX_CRYPTO_POSITIONS:
            return

        # Find market
        market = await self._find_market(slug_prefix, tf_cfg["interval_sec"], tf_cfg["cutoff_sec"])
        if not market or not market.is_active:
            return

        if market.seconds_until_end < tf_cfg["cutoff_sec"]:
            return

        # Already positioned on this market?
        if self._positioned.get(key) == market.condition_id:
            return

        # Don't open a new position if previous one for this slot hasn't closed yet.
        # This prevents overlapping 5m/15m positions for the same crypto.
        old_cid = self._positioned.get(key)
        if old_cid:
            still_open = any(
                p.market_id == old_cid for p in self._portfolio.open_positions
            )
            if still_open:
                return  # wait for previous position to close first
            self._positioned.pop(key, None)

        # Generate signal
        signal = self._generate_signal(snapshot, market, tf_cfg)
        if not signal:
            return

        direction, confidence, reason = signal

        # Fee check — maker orders have 0% fee (vs 1.8% taker)
        # In live mode we use GTC limit orders that rest on the book = maker
        price = market.up_price if direction == "Up" else market.down_price
        maker_fee = 0.0  # maker fee on Polymarket
        expected_edge = abs(confidence * 0.10)
        if expected_edge <= maker_fee + 0.005:  # need at least 0.5% edge after fee
            return

        # Size bet
        bankroll = self._portfolio.bankroll
        if bankroll <= 0:
            return
        max_pct = tf_cfg["max_position_pct"]
        fraction = 0.02 + (max_pct - 0.02) * confidence
        bet_size = round(min(bankroll * fraction, bankroll * max_pct), 2)
        if bet_size > MAX_CRYPTO_BET:
            bet_size = MAX_CRYPTO_BET
        if bet_size < MIN_BET:
            return

        # Execute — use limit price slightly better than market for maker fill
        token_id = market.up_token_id if direction == "Up" else market.down_token_id
        # Place limit 1 tick below market (maker) — better fill, 0% fee
        # In paper mode we simulate at market price (conservative)
        price = market.up_price if direction == "Up" else market.down_price

        if price <= 0.02 or price >= 0.98:
            return

        if not self._paper_mode:
            # LIVE mode guard: this trader has NO CLOB execution path. Never
            # record trades or open positions with real money semantics.
            log.warning(
                "crypto_trader_live_unsupported",
                crypto=crypto_name,
                tf=key,
                hint="btc_trader is paper-only; signal discarded in live mode",
            )
            return

        log.info(
            "crypto_trade_signal",
            crypto=crypto_name,
            tf=key,
            direction=direction,
            confidence=round(confidence, 3),
            reason=reason,
            price_now=snapshot.price,
            change_5m=snapshot.change_5m,
            rsi=snapshot.rsi_14,
            market_price=price,
            bet_size=bet_size,
            seconds_left=round(market.seconds_until_end),
        )

        question = f"{crypto_name} {key.split('_')[1]} {direction} | {market.title[:30]}"
        dir_str = f"BUY_{direction.upper()}"

        # Create DB trade record for auditability
        trade_id = 0
        if self._trade_repo:
            trade = await self._trade_repo.create_trade(
                market_id=market.condition_id,
                token_id=token_id,
                question=question,
                side="BUY",
                direction=dir_str,
                price=price,
                size=bet_size,
                edge=0.0,
                kelly_fraction=0.0,
                estimated_prob=0.0,
                market_price_at_entry=price,
                confidence=confidence,
                status="filled",
                paper_mode=self._paper_mode,
                order_id=f"crypto_{crypto_name}_{key}_{int(_time.time())}",
                reasoning=reason,
            )
            trade_id = trade.id

        # Paper only (live mode returned above). The previous
        # `if self._paper_mode or not self._paper_mode` was always-true and
        # silently opened fictional positions against a live portfolio.
        self._portfolio.open_position(
            trade_id=trade_id,
            market_id=market.condition_id,
            token_id=token_id,
            question=question,
            direction=dir_str,
            price=price,
            size=bet_size,
        )
        self._positioned[key] = market.condition_id
        self._trades_count += 1
        # Record Binance price at entry for resolution
        self._entry_prices[market.condition_id] = (crypto_name, snapshot.price)

        log.info(
            "crypto_paper_trade",
            crypto=crypto_name,
            tf=key,
            direction=direction,
            price=price,
            size=bet_size,
            ends_in=f"{market.seconds_until_end / 60:.1f}min",
            trade_id=trade_id,
        )

    def _generate_signal(
        self, snapshot: BTCSnapshot, market: CryptoMarketInfo, tf_cfg: dict,
    ) -> tuple[str, float, str] | None:
        score = 0.0
        reasons = []
        min_mom = tf_cfg["min_momentum"]

        # 5-min momentum
        if abs(snapshot.change_5m) >= min_mom:
            s = min(abs(snapshot.change_5m) / 0.3, 1.0) * 0.35
            score += s if snapshot.change_5m > 0 else -s
            reasons.append(f"5m {snapshot.change_5m:+.3f}%")

        # 1-min momentum
        if abs(snapshot.change_1m) >= 0.02:
            s = min(abs(snapshot.change_1m) / 0.12, 1.0) * 0.25
            score += s if snapshot.change_1m > 0 else -s
            reasons.append(f"1m {snapshot.change_1m:+.3f}%")

        # RSI — with mean-reversion at extremes
        # Data: RSI > 80 + UP = 44.7% WR (bad), RSI < 20 + DOWN = 62.3% (good)
        # Extreme RSI = reversal signal, not continuation
        rsi = snapshot.rsi_14
        rsi_diff = rsi - 50
        if rsi > 75:
            # Overbought → mean-reversion: bet DOWN
            s = min((rsi - 75) / 20, 1.0) * 0.3
            score -= s  # push score toward DOWN
            reasons.append(f"RSI {rsi:.0f} OB-reversal")
        elif rsi < 25:
            # Oversold → keep momentum DOWN (62.3% WR historically)
            s = min((25 - rsi) / 20, 1.0) * 0.2
            score -= s  # reinforce DOWN
            reasons.append(f"RSI {rsi:.0f} OS-momentum")
        elif abs(rsi_diff) >= MIN_RSI_DIVERGENCE:
            # Normal range: original momentum logic
            s = min(abs(rsi_diff) / 25, 1.0) * 0.2
            score += s if rsi_diff > 0 else -s
            reasons.append(f"RSI {rsi:.0f}")

        # Trend
        if snapshot.trend == "up" and score > 0:
            score += 0.1
            reasons.append("trend=up")
        elif snapshot.trend == "down" and score < 0:
            score -= 0.1
            reasons.append("trend=down")

        # Market mispricing
        if score > 0 and market.up_price < 0.45:
            score += 0.1
            reasons.append(f"Up underpriced({market.up_price:.2f})")
        elif score < 0 and market.down_price < 0.45:
            score -= 0.1
            reasons.append(f"Down underpriced({market.down_price:.2f})")

        if abs(score) < tf_cfg["min_signal"]:
            return None

        direction = "Up" if score > 0 else "Down"
        confidence = min(abs(score), 0.95)
        return direction, confidence, " | ".join(reasons)

    async def _find_market(
        self, slug_prefix: str, interval_sec: int, cutoff_sec: int,
    ) -> CryptoMarketInfo | None:
        now = int(_time.time())
        boundary = now - (now % interval_sec)

        for offset in range(0, 3):
            ts = boundary + offset * interval_sec
            slug = f"{slug_prefix}-{ts}"
            try:
                resp = await self._http.get(
                    f"{GAMMA_API}/events",
                    params={"slug": slug},
                )
                if resp.status_code != 200:
                    continue
                events = resp.json()
                if not events:
                    continue
                event = events[0]
                markets = event.get("markets", [])
                if not markets:
                    continue
                market = CryptoMarketInfo(event, markets[0])
                if market.is_active and market.seconds_until_end > cutoff_sec:
                    return market
            except Exception:
                continue
        return None

    def status(self) -> dict:
        return {
            "running": self._running,
            "cycle_count": self._cycle_count,
            "trades": self._trades_count,
            "wins": self._wins,
            "positioned": dict(self._positioned),
            "assets": list(CRYPTO_ASSETS.keys()),
        }
