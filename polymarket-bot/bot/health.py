import structlog

from data.base import NewsSource
from data.polymarket_rest import PolymarketClient

log = structlog.get_logger()


class HealthChecker:
    def __init__(
        self,
        polymarket: PolymarketClient,
        news_sources: list[NewsSource],
    ) -> None:
        self._polymarket = polymarket
        self._news_sources = news_sources

    async def check_all(self) -> dict:
        results = {
            "polymarket_gamma": False,
            "news_sources": {},
            "healthy": False,
        }

        # Check Polymarket Gamma API
        try:
            results["polymarket_gamma"] = await self._polymarket.gamma_health_check()
        except Exception as e:
            log.error("health_polymarket_error", error=str(e))

        # Check news sources
        healthy_sources = 0
        for source in self._news_sources:
            try:
                ok = await source.health_check()
                results["news_sources"][source.name] = ok
                if ok:
                    healthy_sources += 1
            except Exception:
                results["news_sources"][source.name] = False

        # Overall health: Polymarket + at least 1 news source
        results["healthy"] = results["polymarket_gamma"] and healthy_sources >= 1
        results["healthy_news_sources"] = healthy_sources

        log.info("health_check", **results)
        return results
