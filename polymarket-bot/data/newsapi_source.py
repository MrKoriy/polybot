"""NewsAPI.org — aggregates 80,000+ news sources. Free tier: 100 req/day.

Better coverage than any single source. Supports language/domain filtering.
Free key: https://newsapi.org/register
"""
import time as _time
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from data.base import NewsItem, NewsSource

log = structlog.get_logger()

NEWSAPI_URL = "https://newsapi.org/v2/everything"

# Cache results to stay within 100 req/day free tier.
# With 3-min cycle × 20 markets = 960 potential calls/day.
# Cache for 30 min per query → ~48 unique calls/day.
_CACHE_TTL = 1800  # 30 minutes


class NewsAPISource(NewsSource):
    """NewsAPI.org — wide news aggregator, free tier (100 req/day)."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=15.0)
        # Cache: query_hash -> (items, timestamp)
        self._cache: dict[str, tuple[list[NewsItem], float]] = {}

    @property
    def name(self) -> str:
        return "newsapi"

    async def health_check(self) -> bool:
        try:
            resp = await self._http.get(
                NEWSAPI_URL,
                params={"q": "news", "pageSize": 1, "apiKey": self._api_key},
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def fetch_news(self, query: str, lookback_hours: int = 24) -> list[NewsItem]:
        # Check cache
        cache_key = f"{query[:50]}_{lookback_hours}"
        cached = self._cache.get(cache_key)
        if cached and (_time.monotonic() - cached[1]) < _CACHE_TTL:
            return cached[0]

        try:
            from_date = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime(
                "%Y-%m-%dT%H:%M:%S"
            )

            resp = await self._http.get(
                NEWSAPI_URL,
                params={
                    "q": query,
                    "from": from_date,
                    "sortBy": "publishedAt",
                    "pageSize": 15,
                    "language": "en",
                    "apiKey": self._api_key,
                },
            )

            if resp.status_code == 426:
                # Free tier doesn't support "from" older than 1 month
                log.debug("newsapi_free_tier_limit")
                return []

            if resp.status_code == 429:
                log.warning("newsapi_rate_limited")
                return []

            if resp.status_code != 200:
                log.debug("newsapi_error", status=resp.status_code)
                return []

            data = resp.json()
            articles = data.get("articles", [])
            items = []

            for a in articles:
                title = a.get("title", "")
                if not title or title == "[Removed]":
                    continue

                pub_str = a.get("publishedAt", "")
                try:
                    pub_time = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pub_time = datetime.now(timezone.utc)

                items.append(
                    NewsItem(
                        title=title,
                        body=(a.get("description") or "")[:500],
                        source=f"newsapi/{a.get('source', {}).get('name', 'unknown')}",
                        url=a.get("url", ""),
                        published_at=pub_time,
                    )
                )

            # Update cache
            self._cache[cache_key] = (items, _time.monotonic())

            # Clean old cache entries
            if len(self._cache) > 100:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][1])
                del self._cache[oldest_key]

            log.info("newsapi_fetched", query=query[:50], items=len(items))
            return items

        except Exception:
            log.exception("newsapi_error", query=query[:50])
            return []

    async def close(self) -> None:
        await self._http.aclose()
