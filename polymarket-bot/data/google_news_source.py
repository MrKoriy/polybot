"""Google News RSS source — free, no API key, no rate limits, works on VPS."""
import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
import structlog

from data.base import NewsItem, NewsSource

log = structlog.get_logger()

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"


class GoogleNewsSource(NewsSource):
    """Google News RSS feed — free, no auth, reliable on VPS."""

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; PolymarketBot/1.0)",
                "Accept": "application/rss+xml, application/xml, text/xml",
            },
            follow_redirects=True,
        )

    @property
    def name(self) -> str:
        return "google_news"

    async def health_check(self) -> bool:
        try:
            resp = await self._http.get(
                GOOGLE_NEWS_RSS,
                params={"q": "news", "hl": "en-US", "gl": "US", "ceid": "US:en"},
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def fetch_news(self, query: str, lookback_hours: int = 2) -> list[NewsItem]:
        try:
            # Google News RSS supports "when:Xh" for time-based filtering
            time_filter = f"when:{lookback_hours}h" if lookback_hours <= 24 else f"when:{lookback_hours // 24}d"
            full_query = f"{query} {time_filter}"

            resp = await self._http.get(
                GOOGLE_NEWS_RSS,
                params={
                    "q": full_query,
                    "hl": "en-US",
                    "gl": "US",
                    "ceid": "US:en",
                },
            )

            if resp.status_code != 200:
                log.debug("google_news_http_error", status=resp.status_code)
                return []

            # Parse RSS XML
            root = ET.fromstring(resp.text)
            channel = root.find("channel")
            if channel is None:
                return []

            items = []
            for item_el in channel.findall("item"):
                title = (item_el.findtext("title") or "").strip()
                link = item_el.findtext("link") or ""
                pub_date_str = item_el.findtext("pubDate") or ""
                source_el = item_el.find("source")
                source_name = source_el.text if source_el is not None else "google_news"
                description = (item_el.findtext("description") or "").strip()

                if not title:
                    continue

                # Parse RFC 2822 date
                pub_time = self._parse_rfc2822(pub_date_str)
                if pub_time is None:
                    continue

                # Strip HTML tags from description
                clean_desc = self._strip_html(description)[:500]

                items.append(
                    NewsItem(
                        title=title,
                        body=clean_desc,
                        source=f"google_news/{source_name}",
                        url=link,
                        published_at=pub_time,
                    )
                )

            log.info("google_news_fetched", query=query[:50], items=len(items))
            return items[:20]

        except Exception:
            log.exception("google_news_error", query=query[:50])
            return []

    @staticmethod
    def _parse_rfc2822(date_str: str) -> datetime | None:
        if not date_str:
            return None
        try:
            dt = parsedate_to_datetime(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    @staticmethod
    def _strip_html(text: str) -> str:
        """Remove HTML tags from text."""
        import re
        return re.sub(r"<[^>]+>", "", text).strip()

    async def close(self) -> None:
        await self._http.aclose()
