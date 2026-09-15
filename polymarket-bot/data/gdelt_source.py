"""GDELT Project news source — free, no API key required, 15-min global event detection."""
from datetime import datetime, timezone

import httpx
import structlog

from data.base import NewsItem, NewsSource

log = structlog.get_logger()

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"


class GdeltSource(NewsSource):
    """GDELT Project v2 Doc API — free global news monitor, no API key needed.

    Returns the most recent articles mentioning a query. Covers 65 languages
    across 65,000+ global news outlets with 15-minute event detection.
    """

    def __init__(self, max_records: int = 25) -> None:
        self._http = httpx.AsyncClient(timeout=20.0)
        self._max_records = max_records
        self._last_request = 0.0  # monotonic timestamp of last request

    @property
    def name(self) -> str:
        return "gdelt"

    async def health_check(self) -> bool:
        try:
            resp = await self._http.get(
                GDELT_DOC_API,
                params={"query": "news", "mode": "artlist", "maxrecords": 1, "format": "json"},
                timeout=25.0,
            )
            if resp.status_code != 200:
                return False
            # GDELT returns 200 with rate-limit text in body instead of 429
            if "limit requests" in resp.text[:200].lower():
                return False
            return True
        except Exception:
            return False

    async def fetch_news(self, query: str, lookback_hours: int = 2) -> list[NewsItem]:
        try:
            import asyncio, time as _time
            # GDELT requires minimum 5 seconds between requests
            elapsed = _time.monotonic() - self._last_request
            if elapsed < 6.0:
                await asyncio.sleep(6.0 - elapsed)
            self._last_request = _time.monotonic()
            # Map lookback_hours to GDELT timespan string
            if lookback_hours <= 1:
                timespan = "60min"
            elif lookback_hours <= 6:
                timespan = f"{lookback_hours * 60}min"
            elif lookback_hours <= 24:
                timespan = f"{lookback_hours}h"
            else:
                timespan = f"{min(lookback_hours // 24, 7)}d"

            resp = await self._http.get(
                GDELT_DOC_API,
                params={
                    "query": query,
                    "mode": "artlist",
                    "maxrecords": self._max_records,
                    "timespan": timespan,
                    "sort": "DateDesc",
                    "format": "json",
                },
            )
            # GDELT returns 200 with text body on rate limit, not a proper 429
            if resp.status_code == 429 or "limit requests" in resp.text[:100].lower():
                log.debug("gdelt_rate_limited", query=query[:50])
                self._last_request = _time.monotonic()  # Reset timer
                return []
            resp.raise_for_status()
            data = resp.json()

            articles = data.get("articles", []) or []
            now = datetime.now(timezone.utc)
            items = []

            for article in articles:
                title = article.get("title", "").strip()
                url = article.get("url", "")
                domain = article.get("domain", "gdelt")
                seendate = article.get("seendate", "")

                if not title:
                    continue

                # Parse GDELT date format: "20250317T120000Z"
                pub_time = _parse_gdelt_date(seendate)
                if pub_time is None:
                    continue  # Skip articles with unparseable dates

                items.append(
                    NewsItem(
                        title=title,
                        body=article.get("seendescription", "")[:400],
                        source=f"gdelt/{domain}",
                        url=url,
                        published_at=pub_time,
                    )
                )

            log.info("gdelt_fetched", query=query[:50], items=len(items))
            return items

        except Exception:
            log.exception("gdelt_error", query=query[:50])
            return []

    async def close(self) -> None:
        await self._http.aclose()


def _parse_gdelt_date(date_str: str) -> datetime | None:
    """Parse GDELT date format: '20250317T120000Z'."""
    if not date_str:
        return None
    try:
        # Format: YYYYMMDDTHHMMSSZ
        clean = date_str.replace("Z", "").replace("T", "")
        if len(clean) >= 14:
            return datetime(
                int(clean[0:4]),
                int(clean[4:6]),
                int(clean[6:8]),
                int(clean[8:10]),
                int(clean[10:12]),
                int(clean[12:14]),
                tzinfo=timezone.utc,
            )
        elif len(clean) == 8:
            return datetime(
                int(clean[0:4]), int(clean[4:6]), int(clean[6:8]),
                tzinfo=timezone.utc,
            )
    except (ValueError, IndexError):
        pass
    return None
