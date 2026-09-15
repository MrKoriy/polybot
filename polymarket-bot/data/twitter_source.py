"""Twitter/X news source using tweepy v4.

Requires Twitter API v2 access token (TWITTER_BEARER_TOKEN env var).
Without a token, this source is silently disabled.

Twitter API v2 free tier is very limited; to enable full functionality,
set TWITTER_BEARER_TOKEN in your .env file.
"""
from datetime import datetime, timezone

import structlog

from data.base import NewsItem, NewsSource

log = structlog.get_logger()


class TwitterSource(NewsSource):
    """Twitter/X recent tweets source via tweepy v4 (Twitter API v2)."""

    def __init__(self, bearer_token: str = "") -> None:
        self._bearer_token = bearer_token
        self._client = None
        if bearer_token:
            try:
                import tweepy
                self._client = tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=True)
                log.info("twitter_source_initialized")
            except ImportError:
                log.warning("twitter_skip", reason="tweepy not installed; run: pip install tweepy")
            except Exception as e:
                log.warning("twitter_init_error", error=str(e))

    @property
    def name(self) -> str:
        return "twitter"

    async def health_check(self) -> bool:
        return self._client is not None

    async def fetch_news(self, query: str, lookback_hours: int = 2) -> list[NewsItem]:
        if not self._client:
            return []

        try:
            import asyncio
            from datetime import timedelta

            start_time = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

            # Use asyncio.to_thread since tweepy v4 is synchronous
            tweets = await asyncio.to_thread(
                self._search_recent, query, start_time
            )
            return tweets

        except Exception as e:
            log.warning("twitter_fetch_error", query=query[:50], error=str(e))
            return []

    def _search_recent(self, query: str, start_time: datetime) -> list[NewsItem]:
        """Synchronous Twitter search — wrapped in asyncio.to_thread by caller."""
        try:
            # Exclude retweets, limit to English
            full_query = f"{query} -is:retweet lang:en"
            response = self._client.search_recent_tweets(
                query=full_query,
                max_results=20,
                start_time=start_time,
                tweet_fields=["created_at", "author_id", "text"],
            )
            if response is None or not response.data:
                return []

            items = []
            for tweet in response.data:
                pub_time = tweet.created_at or datetime.now(timezone.utc)
                if pub_time.tzinfo is None:
                    pub_time = pub_time.replace(tzinfo=timezone.utc)
                items.append(
                    NewsItem(
                        title=tweet.text[:200],
                        body=tweet.text,
                        source="twitter",
                        url=f"https://twitter.com/i/web/status/{tweet.id}",
                        published_at=pub_time,
                    )
                )
            log.info("twitter_fetched", query=query[:50], items=len(items))
            return items

        except Exception as e:
            log.warning("twitter_search_error", error=str(e))
            return []

    async def close(self) -> None:
        pass
