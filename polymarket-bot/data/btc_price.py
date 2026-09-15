"""Crypto price feed from Binance — free, no API key, supports BTC/ETH/SOL."""
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import structlog

log = structlog.get_logger()

# Use binance.vision — works from US IPs (api.binance.com returns 451)
BINANCE_BASE = "https://data-api.binance.vision/api/v3"


@dataclass
class BTCSnapshot:
    """Price snapshot with momentum indicators. Works for any crypto."""
    symbol: str
    price: float
    timestamp: datetime
    change_1m: float   # % change over last 1 min
    change_5m: float   # % change over last 5 min
    change_15m: float  # % change over last 15 min
    volume_1m: float   # volume in last 1 min candle
    rsi_14: float      # 14-period RSI on 1-min candles
    trend: str          # "up", "down", "flat"


class BTCPriceFeed:
    """Real-time crypto price and momentum from Binance. Supports any USDT pair."""

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=10.0)

    async def get_snapshot(self, symbol: str = "BTCUSDT") -> BTCSnapshot | None:
        """Get current price with momentum indicators for any Binance symbol."""
        try:
            resp = await self._http.get(
                f"{BINANCE_BASE}/klines",
                params={"symbol": symbol, "interval": "1m", "limit": 20},
            )
            resp.raise_for_status()
            candles = resp.json()

            if len(candles) < 15:
                return None

            closes = [float(c[4]) for c in candles]
            volumes = [float(c[5]) for c in candles]
            current_price = closes[-1]
            now = datetime.now(timezone.utc)

            change_1m = (closes[-1] - closes[-2]) / closes[-2] * 100 if closes[-2] else 0
            change_5m = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else 0
            change_15m = (closes[-1] - closes[-16]) / closes[-16] * 100 if len(closes) >= 16 else 0

            volume_1m = volumes[-1]
            rsi = self._calc_rsi(closes, 14)

            if change_5m > 0.1 and change_1m > 0:
                trend = "up"
            elif change_5m < -0.1 and change_1m < 0:
                trend = "down"
            else:
                trend = "flat"

            return BTCSnapshot(
                symbol=symbol,
                price=current_price,
                timestamp=now,
                change_1m=round(change_1m, 4),
                change_5m=round(change_5m, 4),
                change_15m=round(change_15m, 4),
                volume_1m=round(volume_1m, 4),
                rsi_14=round(rsi, 2),
                trend=trend,
            )

        except Exception:
            log.debug("crypto_price_error", symbol=symbol)
            return None

    @staticmethod
    def _calc_rsi(closes: list[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0

        gains = []
        losses = []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    async def close(self) -> None:
        await self._http.aclose()
