"""Market resolution service — the single honest source of paper-mode PnL.

RULE: paper positions may only close at the ACTUAL market resolution
(Gamma `resolved` + final outcomePrices/outcome), or at a real CLOB
mid price if we must exit early. Bot-internal oracles (Binance spot vs
threshold, METAR readings) are advisory diagnostics only — they never
determine PnL. The prior design resolved paper trades via those oracles
at exactly $1/$0, which fabricated the "100% WR" paper results that
then lost real money in live mode.
"""

import json
import time as _time

import httpx
import structlog

log = structlog.get_logger()

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Gamma marks markets resolved a few minutes before final prices propagate;
# poll modestly rather than hammering.
_CACHE_TTL_SEC = 30.0
# Direction tokens that map to outcome index 0 ("Yes"/"Up"/first bucket).
_SIDE0_MARKERS = ("YES", "UP")


class MarketResolutionService:
    """Resolves positions against actual Polymarket market outcomes.

    `polymarket_client` (optional): any object exposing async
    get_market_resolution(condition_id) / get_midpoint(token_id) — e.g.
    data.polymarket_rest.PolymarketClient. When absent, the service queries
    Gamma directly over public REST so paper traders can use it standalone.
    """

    def __init__(self, polymarket_client=None) -> None:
        self._polymarket = polymarket_client
        self._own_http = polymarket_client is None
        self._http = (
            httpx.AsyncClient(
                timeout=10.0,
                headers={"User-Agent": "Mozilla/5.0 (compatible; PolyBot/1.0)"},
            )
            if self._own_http
            else None
        )
        # condition_id -> (monotonic_ts, resolution_dict)
        self._cache: dict[str, tuple[float, dict]] = {}

    async def close(self) -> None:
        if self._own_http and self._http:
            await self._http.aclose()

    async def get_resolution(self, condition_id: str) -> dict:
        """{"resolved": bool, "yes_won": bool | None} — cached 30s.

        `yes_won` refers to outcome index 0 (the "Yes"/"Up" token, i.e.
        clobTokenIds[0]). Never invents an outcome: unresolved or ambiguous
        markets return resolved=False and the caller must keep holding.
        """
        now = _time.monotonic()
        cached = self._cache.get(condition_id)
        if cached and (now - cached[0]) < _CACHE_TTL_SEC:
            return cached[1]

        if self._polymarket is not None:
            result = await self._polymarket.get_market_resolution(condition_id)
        else:
            result = await self._gamma_resolution(condition_id)

        if result.get("resolved"):
            # Resolution is terminal — cache forever.
            self._cache[condition_id] = (float("inf"), result)
        else:
            self._cache[condition_id] = (now, result)
        return result

    async def _gamma_resolution(self, condition_id: str) -> dict:
        """CLOB /markets/{cid} PRIMARY (token winner flags = settlement truth),
        Gamma outcomePrices fallback.

        The Gamma ?conditionId= query returns STALE data for closed markets
        (verified Sep 2026: closed market showed 0.0485/0.9515 there while the
        true outcome was 0/1) — never trust it as the resolution source.
        """
        try:
            resp = await self._http.get(f"{CLOB_API}/markets/{condition_id}")
            if resp.status_code == 200:
                clob = resp.json()
                tokens = clob.get("tokens") or []
                if clob.get("closed") and len(tokens) >= 2:
                    winners = [t for t in tokens if t.get("winner")]
                    if len(winners) == 1:
                        yes_won = str(winners[0].get("token_id") or "") == str(
                            tokens[0].get("token_id") or ""
                        )
                        return {"resolved": True, "yes_won": yes_won}
                    return {"resolved": False, "yes_won": None}
        except Exception as e:
            log.debug("resolution_clob_error", condition_id=condition_id[:20], error=str(e))

        try:
            resp = await self._http.get(
                f"{GAMMA_API}/markets",
                params={"conditionId": condition_id, "limit": 1},
            )
            if resp.status_code != 200:
                return {"resolved": False, "yes_won": None}
            markets = resp.json()
            if not markets:
                return {"resolved": False, "yes_won": None}
            data = markets[0] if isinstance(markets, list) else markets
            if data.get("conditionId") != condition_id:
                return {"resolved": False, "yes_won": None}

            resolved = bool(data.get("resolved", False))
            raw_prices = data.get("outcomePrices", "")
            if isinstance(raw_prices, str) and raw_prices:
                price_list = json.loads(raw_prices)
            elif isinstance(raw_prices, list):
                price_list = raw_prices
            else:
                price_list = []

            if price_list:
                try:
                    p0 = float(price_list[0])
                    if p0 in (0.0, 1.0):
                        return {"resolved": True, "yes_won": p0 == 1.0}
                except (TypeError, ValueError):
                    pass

            outcome = (data.get("outcome", "") or "").upper()
            if resolved and outcome == "YES":
                return {"resolved": True, "yes_won": True}
            if resolved and outcome == "NO":
                return {"resolved": True, "yes_won": False}
            return {"resolved": False, "yes_won": None}
        except Exception as e:
            log.debug("resolution_gamma_error", condition_id=condition_id[:20], error=str(e))
            return {"resolved": False, "yes_won": None}

    def exit_price_for_direction(self, resolution: dict, direction: str) -> float | None:
        """Map a market resolution to a payout for our position direction.

        Returns 1.0 / 0.0 when resolved and the direction is mappable,
        otherwise None (keep holding). Never returns 0.5.
        """
        if not resolution.get("resolved"):
            return None
        yes_won = resolution.get("yes_won")
        if yes_won is None:
            return None
        dir_upper = (direction or "").upper()
        we_hold_side0 = any(m in dir_upper for m in _SIDE0_MARKERS)
        if we_hold_side0:
            return 1.0 if yes_won else 0.0
        return 0.0 if yes_won else 1.0

    async def resolve_position(self, position) -> tuple[bool, float | None, str]:
        """Check a portfolio Position against actual market resolution.

        Returns (resolved, exit_price, note). exit_price is the payout for
        OUR direction (1.0/0.0). Unresolved → (False, None, note).
        """
        res = await self.get_resolution(position.market_id)
        exit_price = self.exit_price_for_direction(res, position.direction or "")
        if exit_price is None:
            return False, None, "not_resolved_or_ambiguous"
        return True, exit_price, "resolved_on_market"

    async def market_exit_price(self, token_id: str) -> float | None:
        """Real CLOB mid price for early exits (risk cleanup), never $1/$0.

        Requires a polymarket_client (public midpoint endpoint); standalone
        mode returns None.
        """
        if self._polymarket is None:
            return None
        try:
            return await self._polymarket.get_midpoint(token_id)
        except Exception:
            return None
