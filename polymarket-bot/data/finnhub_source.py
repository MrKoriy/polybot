from datetime import datetime, timedelta, timezone

import httpx
import structlog

from data.base import NewsItem, NewsSource

log = structlog.get_logger()

FINNHUB_BASE = "https://finnhub.io/api/v1"


class FinnhubSource(NewsSource):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=15.0)

    @property
    def name(self) -> str:
        return "finnhub"

    async def health_check(self) -> bool:
        if not self._api_key:
            return False
        try:
            resp = await self._http.get(
                f"{FINNHUB_BASE}/news",
                params={"category": "general", "token": self._api_key},
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def fetch_news(self, query: str, lookback_hours: int = 2) -> list[NewsItem]:
        if not self._api_key:
            log.debug("finnhub_skip", reason="no API key")
            return []

        try:
            now = datetime.now(timezone.utc)
            from_date = (now - timedelta(hours=max(lookback_hours, 24))).strftime("%Y-%m-%d")
            to_date = now.strftime("%Y-%m-%d")

            # General news
            resp = await self._http.get(
                f"{FINNHUB_BASE}/news",
                params={
                    "category": "general",
                    "minId": 0,
                    "token": self._api_key,
                },
            )
            resp.raise_for_status()
            articles = resp.json()

            cutoff = now - timedelta(hours=lookback_hours)
            items = []
            query_lower = query.lower()
            query_words = set(w for w in query_lower.split() if len(w) > 2)

            for article in articles:
                # Filter by time
                pub_ts = article.get("datetime", 0)
                if not pub_ts:
                    continue
                pub_time = datetime.fromtimestamp(pub_ts, tz=timezone.utc)
                if pub_time < cutoff:
                    continue

                title = article.get("headline", "")
                summary = article.get("summary", "")
                combined = f"{title} {summary}".lower()

                # Check relevance: at least some query words appear
                if query_words:
                    matching_words = sum(1 for w in query_words if w in combined)
                    if matching_words < max(1, len(query_words) // 3):
                        continue
                    relevance = matching_words / len(query_words)
                else:
                    relevance = 0.0

                items.append(
                    NewsItem(
                        title=title,
                        body=summary[:500],
                        source="finnhub",
                        url=article.get("url", ""),
                        published_at=pub_time,
                        relevance_score=relevance,
                    )
                )

            log.info("finnhub_fetched", query=query[:50], items=len(items))
            return items[:20]

        except Exception:
            log.exception("finnhub_error")
            return []

    async def close(self) -> None:
        await self._http.aclose()
