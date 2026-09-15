import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from data.base import NewsItem, NewsSource

log = structlog.get_logger()

# Reddit JSON endpoints — no API key needed
REDDIT_SEARCH_URL = "https://www.reddit.com/search.json"
REDDIT_SUB_SEARCH_URL = "https://www.reddit.com/r/{subreddit}/search.json"
REDDIT_SUB_NEW_URL = "https://www.reddit.com/r/{subreddit}/new.json"

# PullPush.io — Pushshift replacement, free full-text Reddit search
PULLPUSH_URL = "https://api.pullpush.io/reddit/search/submission"

# Subreddits relevant for prediction markets
RELEVANT_SUBREDDITS = [
    "polymarket", "predictions", "wallstreetbets",
    "politics", "worldnews", "cryptocurrency",
    "news", "economics", "geopolitics",
]

# Mimic a real browser to avoid blocks
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


class RedditSource(NewsSource):
    """Fetches Reddit posts via public JSON endpoints — no API key required."""

    def __init__(self, user_agent: str = "") -> None:
        self._http = httpx.AsyncClient(
            timeout=15.0,
            headers=HEADERS,
            follow_redirects=True,
        )
        self._request_delay = 2.0  # seconds between requests to avoid rate limiting

    @property
    def name(self) -> str:
        return "reddit"

    async def health_check(self) -> bool:
        """Check PullPush (reliable) — Reddit JSON endpoints are blocked by anti-bot."""
        try:
            resp = await self._http.get(
                PULLPUSH_URL,
                params={"q": "news", "size": 1},
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def fetch_news(self, query: str, lookback_hours: int = 2) -> list[NewsItem]:
        items: list[NewsItem] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

        # Strategy 1: PullPush.io — reliable, always works
        pp_items = await self._pullpush_search(query, cutoff)
        items.extend(pp_items)

        # Strategy 2: Reddit global search (may fail — anti-bot blocks)
        search_items = await self._reddit_search(query, cutoff)
        items.extend(search_items)

        # Strategy 3: Search specific subreddits (may fail — same reason)
        if not search_items:  # Only if Reddit search failed
            sub_items = await self._search_subreddits(query, cutoff)
            items.extend(sub_items)

        # Deduplicate by URL
        seen = set()
        unique = []
        for item in items:
            key = item.url.rstrip("/")
            if key not in seen:
                seen.add(key)
                unique.append(item)

        # Sort by time, newest first
        unique.sort(key=lambda x: x.published_at, reverse=True)

        log.info("reddit_fetched", query=query[:50], items=len(unique))
        return unique[:20]

    async def _reddit_search(self, query: str, cutoff: datetime) -> list[NewsItem]:
        """Global Reddit search via public JSON endpoint."""
        try:
            resp = await self._http.get(
                REDDIT_SEARCH_URL,
                params={
                    "q": query,
                    "sort": "new",
                    "t": "day",
                    "limit": 15,
                    "type": "link",
                },
            )

            if resp.status_code == 429:
                log.warning("reddit_rate_limited", endpoint="search")
                return []

            if resp.status_code != 200:
                log.debug("reddit_search_error", status=resp.status_code)
                return []

            data = resp.json()
            return self._parse_listing(data, cutoff, "reddit/search")

        except Exception:
            log.exception("reddit_search_error")
            return []

    async def _search_subreddits(self, query: str, cutoff: datetime) -> list[NewsItem]:
        """Search specific subreddits via JSON endpoints."""
        items = []

        # Pick top 4 most relevant subreddits to avoid too many requests
        subs_to_search = RELEVANT_SUBREDDITS[:4]

        for sub in subs_to_search:
            try:
                await asyncio.sleep(self._request_delay)

                resp = await self._http.get(
                    REDDIT_SUB_SEARCH_URL.format(subreddit=sub),
                    params={
                        "q": query,
                        "sort": "new",
                        "restrict_sr": "on",
                        "t": "day",
                        "limit": 5,
                    },
                )

                if resp.status_code == 429:
                    log.warning("reddit_rate_limited", subreddit=sub)
                    break  # Stop hitting Reddit if rate limited

                if resp.status_code != 200:
                    continue

                data = resp.json()
                sub_items = self._parse_listing(data, cutoff, f"reddit/r/{sub}")
                items.extend(sub_items)

            except Exception:
                log.debug("reddit_sub_error", subreddit=sub)
                continue

        return items

    async def _pullpush_search(self, query: str, cutoff: datetime) -> list[NewsItem]:
        """Search via PullPush.io (Pushshift replacement) — free, no auth."""
        try:
            after_ts = int(cutoff.timestamp())

            resp = await self._http.get(
                PULLPUSH_URL,
                params={
                    "q": query,
                    "after": after_ts,
                    "sort": "desc",
                    "sort_type": "created_utc",
                    "size": 15,
                },
            )

            if resp.status_code != 200:
                log.debug("pullpush_error", status=resp.status_code)
                return []

            data = resp.json()
            items = []

            for post in data.get("data", []):
                try:
                    created = post.get("created_utc", 0)
                    pub_time = datetime.fromtimestamp(created, tz=timezone.utc)

                    if pub_time < cutoff:
                        continue

                    title = post.get("title", "")
                    body = (post.get("selftext", "") or "")[:500]
                    subreddit = post.get("subreddit", "unknown")
                    permalink = post.get("permalink", "")
                    score = post.get("score", 0)
                    num_comments = post.get("num_comments", 0)

                    items.append(
                        NewsItem(
                            title=title,
                            body=body,
                            source=f"reddit/r/{subreddit}",
                            url=f"https://reddit.com{permalink}" if permalink else "",
                            published_at=pub_time,
                            relevance_score=score / max(num_comments + 1, 1),
                        )
                    )
                except (KeyError, ValueError, TypeError):
                    continue

            log.debug("pullpush_fetched", items=len(items))
            return items

        except Exception:
            log.debug("pullpush_error")
            return []

    def _parse_listing(self, data: dict, cutoff: datetime, source: str) -> list[NewsItem]:
        """Parse Reddit listing JSON response."""
        items = []

        listing = data.get("data", {})
        children = listing.get("children", [])

        for child in children:
            try:
                post = child.get("data", {})
                created = post.get("created_utc", 0)
                pub_time = datetime.fromtimestamp(created, tz=timezone.utc)

                if pub_time < cutoff:
                    continue

                title = post.get("title", "")
                body = (post.get("selftext", "") or "")[:500]
                subreddit = post.get("subreddit", "")
                permalink = post.get("permalink", "")
                score = post.get("score", 0)
                num_comments = post.get("num_comments", 0)

                # Use subreddit-specific source if available
                post_source = f"reddit/r/{subreddit}" if subreddit else source

                items.append(
                    NewsItem(
                        title=title,
                        body=body,
                        source=post_source,
                        url=f"https://reddit.com{permalink}" if permalink else "",
                        published_at=pub_time,
                        relevance_score=score / max(num_comments + 1, 1),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue

        return items

    async def close(self) -> None:
        await self._http.aclose()
