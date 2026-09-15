"""Binance WebSocket feed — real-time BTC/ETH/SOL price ticks.

Sub-millisecond price updates via persistent WebSocket connection.
Used as leading indicator for Polymarket crypto market movements.
"""
import asyncio
import json
import time

import structlog
import websockets

log = structlog.get_logger()

# Use binance.vision — works from US IPs (stream.binance.com returns 451)
BINANCE_WS = "wss://data-stream.binance.vision/ws"

# Symbols to track
SYMBOLS = {
    "btcusdt": "BTC",
    "ethusdt": "ETH",
    "solusdt": "SOL",
    "xrpusdt": "XRP",
    "dogeusdt": "DOGE",
}


class BinanceWSFeed:
    """Real-time crypto prices from Binance WebSocket."""

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}       # "BTC" -> 65000.0
        self._prev_prices: dict[str, float] = {}   # previous tick for momentum
        self._last_update: dict[str, float] = {}   # monotonic timestamp
        self._running = False
        self._ws = None

    @property
    def prices(self) -> dict[str, float]:
        return dict(self._prices)

    def get_price(self, symbol: str) -> float | None:
        """Get latest price for BTC/ETH/SOL."""
        return self._prices.get(symbol)

    def get_change_pct(self, symbol: str) -> float:
        """Get instant tick change %."""
        curr = self._prices.get(symbol, 0)
        prev = self._prev_prices.get(symbol, curr)
        if prev == 0:
            return 0.0
        return (curr - prev) / prev * 100

    def age_ms(self, symbol: str) -> float:
        """How old is the latest price in milliseconds."""
        ts = self._last_update.get(symbol, 0)
        if ts == 0:
            return float("inf")
        return (time.monotonic() - ts) * 1000

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._run_loop())
        log.info("binance_ws_started", symbols=list(SYMBOLS.values()))

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def _run_loop(self) -> None:
        reconnect_delay = 1.0
        while self._running:
            try:
                # Subscribe to mini-ticker streams for all symbols
                streams = "/".join(f"{s}@miniTicker" for s in SYMBOLS.keys())
                url = f"{BINANCE_WS}/{streams}"

                async with websockets.connect(url, ping_interval=20) as ws:
                    self._ws = ws
                    reconnect_delay = 1.0
                    log.info("binance_ws_connected")

                    async for raw in ws:
                        if not self._running:
                            break
                        self._handle_message(raw)

            except websockets.ConnectionClosed:
                log.warning("binance_ws_disconnected")
            except Exception:
                log.exception("binance_ws_error")

            if self._running:
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
            symbol_lower = data.get("s", "").lower()
            crypto = SYMBOLS.get(symbol_lower)
            if not crypto:
                return

            price = float(data.get("c", 0))  # "c" = close price in miniTicker
            if price <= 0:
                return

            self._prev_prices[crypto] = self._prices.get(crypto, price)
            self._prices[crypto] = price
            self._last_update[crypto] = time.monotonic()

        except (json.JSONDecodeError, ValueError, KeyError):
            pass
