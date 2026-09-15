import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from data.base import NewsItem, NewsSource

log = structlog.get_logger()

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/news/search"


class BraveSearchSource(NewsSource):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=15.0)
        self._last_request_time = 0.0

    @property
    def name(self) -> str:
        return "brave_search"

    async def health_check(self) -> bool:
        if not self._api_key:
            return False
        try:
            resp = await self._http.get(
                BRAVE_SEARCH_URL,
                headers=self._headers(),
                params={"q": "test", "count": 1},
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def fetch_news(self, query: str, lookback_hours: int = 2) -> list[NewsItem]:
        if not self._api_key:
            log.debug("brave_skip", reason="no API key")
            return []

        try:
            import time

            data = None
            for attempt in range(2):  # Only 2 attempts to save rate limit quota
                # Rate limit: free tier ~1 req/sec, enforce 5s minimum between calls
                now = time.monotonic()
                elapsed = now - self._last_request_time
                min_wait = 5.0 * (attempt + 1)  # 5s, 10s backoff
                if elapsed < min_wait:
                    await asyncio.sleep(min_wait - elapsed)
                self._last_request_time = time.monotonic()

                resp = await self._http.get(
                    BRAVE_SEARCH_URL,
                    headers=self._headers(),
                    params={
                        "q": query,
                        "count": 10,
                        "freshness": "pw",  # past week
                    },
                )
                if resp.status_code == 429:
                    log.debug("brave_rate_limited", attempt=attempt + 1)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break

            if data is None:
                log.warning("brave_rate_limit_exhausted", query=query[:50])
                return []

            cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
            items = []

            for result in data.get("results", []):
                # Parse published date
                pub_str = result.get("age", "") or result.get("page_age", "")
                pub_time = self._parse_age(pub_str)

                if pub_time and pub_time < cutoff:
                    continue

                title = result.get("title", "")
                description = result.get("description", "")

                items.append(
                    NewsItem(
                        title=title,
                        body=description[:500],
                        source="brave_search",
                        url=result.get("url", ""),
                        published_at=pub_time or datetime.now(timezone.utc),
                    )
                )

            log.info("brave_fetched", query=query[:50], items=len(items))
            return items

        except Exception:
            log.exception("brave_error")
            return []

    def _headers(self) -> dict:
        return {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self._api_key,
        }

    @staticmethod
    def _parse_age(age_str: str) -> datetime | None:
        if not age_str:
            return None

        import re

        now = datetime.now(timezone.utc)
        age_str = age_str.lower().strip()

        try:
            # Extract the first number from the string
            match = re.search(r"(\d+)", age_str)
            num = int(match.group(1)) if match else 1

            if "hour" in age_str:
                return now - timedelta(hours=num)
            elif "minute" in age_str or "min" in age_str:
                return now - timedelta(minutes=num)
            elif "day" in age_str:
                return now - timedelta(days=num)
            elif "second" in age_str:
                return now
            elif "week" in age_str:
                return now - timedelta(weeks=num)
        except (ValueError, IndexError):
            pass

        return None

    async def close(self) -> None:
        await self._http.aclose()
