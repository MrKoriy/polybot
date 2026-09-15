import asyncio
import json
from collections import defaultdict

import structlog
import websockets

from core.event_bus import EventBus

log = structlog.get_logger()

# Heartbeat interval — Polymarket CLOB WS requires PING to keep connection alive
_HEARTBEAT_INTERVAL = 30  # seconds


class MarketWebSocket:
    def __init__(self, ws_url: str, event_bus: EventBus) -> None:
        self._ws_url = ws_url
        self._event_bus = event_bus
        self._ws = None
        self._running = False
        self._subscribed_assets: set[str] = set()
        self._prices: dict[str, float] = {}
        self._reconnect_delay = 1.0

    @property
    def prices(self) -> dict[str, float]:
        return dict(self._prices)

    def get_price(self, token_id: str) -> float | None:
        return self._prices.get(token_id)

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def subscribe(self, token_ids: list[str]) -> None:
        new_ids = set(token_ids) - self._subscribed_assets
        if not new_ids:
            return

        self._subscribed_assets.update(new_ids)

        if self._ws:
            for asset_id in new_ids:
                # CLOB WS subscription format
                msg = {
                    "auth": {},
                    "type": "subscribe",
                    "channel": "market",
                    "markets": [asset_id],
                }
                try:
                    await self._ws.send(json.dumps(msg))
                except Exception:
                    log.debug("ws_subscribe_error", asset_id=asset_id)

    async def unsubscribe(self, token_ids: list[str]) -> None:
        removed = set(token_ids) & self._subscribed_assets
        self._subscribed_assets -= removed
        for tid in removed:
            self._prices.pop(tid, None)
            if self._ws:
                msg = {
                    "type": "unsubscribe",
                    "channel": "market",
                    "markets": [tid],
                }
                try:
                    await self._ws.send(json.dumps(msg))
                except Exception:
                    pass

    async def _run_loop(self) -> None:
        while self._running:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1.0
                    log.info("ws_connected", url=self._ws_url)

                    # Start heartbeat task
                    heartbeat_task = asyncio.create_task(self._heartbeat(ws))

                    try:
                        # Re-subscribe to tracked assets
                        for asset_id in self._subscribed_assets:
                            msg = {
                                "auth": {},
                                "type": "subscribe",
                                "channel": "market",
                                "markets": [asset_id],
                            }
                            await ws.send(json.dumps(msg))

                        async for raw_msg in ws:
                            if not self._running:
                                break
                            await self._handle_message(raw_msg)
                    finally:
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except asyncio.CancelledError:
                            pass

            except websockets.ConnectionClosed:
                log.warning("ws_disconnected")
            except Exception:
                log.exception("ws_error")

            if self._running:
                log.info("ws_reconnecting", delay=self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60.0)

    async def _heartbeat(self, ws) -> None:
        """Send periodic PING to keep WS connection alive."""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            try:
                await ws.ping()
            except Exception:
                log.debug("ws_heartbeat_failed")
                break

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type") or data.get("event_type", "")

        if msg_type in ("price_change", "book", "last_trade_price"):
            changes = data.get("changes", [data]) if msg_type != "last_trade_price" else [data]
            for change in changes:
                asset_id = change.get("asset_id") or change.get("token_id") or change.get("market", "")
                price = change.get("price")
                if asset_id and price is not None:
                    old_price = self._prices.get(asset_id)
                    new_price = float(price)
                    self._prices[asset_id] = new_price

                    # Emit event on significant price change (>2%)
                    if old_price and old_price > 0 and abs(new_price - old_price) / old_price > 0.02:
                        await self._event_bus.publish(
                            "price_update",
                            asset_id=asset_id,
                            old_price=old_price,
                            new_price=new_price,
                        )
