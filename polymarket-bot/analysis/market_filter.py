from datetime import datetime, timedelta

import structlog

from data.base import MarketCandidate

log = structlog.get_logger()

# Shared keyword map — also used by MetaStrategy for category classification.
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "nba": ["vs.", "spread", "o/u", "moneyline", "nba", "lakers", "celtics",
            "warriors", "bucks", "nuggets", "76ers", "cavaliers", "thunder",
            "timberwolves", "mavericks", "pistons", "raptors", "knicks", "heat"],
    "nfl": ["nfl", "touchdown", "quarterback", "super bowl", "chiefs", "eagles"],
    "soccer": ["fc", "premier league", "la liga", "champions league",
               "liverpool", "arsenal", "city", "barcelona", "real madrid", "tottenham"],
    "hockey": ["nhl", "canadiens", "ducks", "rangers", "bruins", "penguins"],
    "esports": ["valorant", "dota", "league of legends", "csgo", "cs2", "bo3", "bo5"],
    "mma": ["ufc", "bellator", "mma", "fighter"],
    "politics": ["trump", "biden", "election", "congress", "senate", "vote"],
    "crypto": ["bitcoin", "ethereum", "btc", "eth", "crypto"],
    "finance": ["oil", "s&p", "nasdaq", "stock", "fed", "rate"],
}


def classify_market_question(question: str) -> str:
    """Classify a market question into a category."""
    q = question.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return category
    return "other"


class MarketFilter:
    def __init__(
        self,
        min_volume: float = 5000,
        max_days_to_resolution: int = 7,
        max_hours_to_resolution: int = 48,
        max_spread: float = 0.15,
        top_n: int = 15,
    ) -> None:
        self._min_volume = min_volume
        self._max_days = max_days_to_resolution
        self._max_hours = max_hours_to_resolution
        self._max_spread = max_spread
        self._top_n = top_n
        # Meta-strategy category preferences (updated at runtime by MetaStrategy)
        self._preferred_categories: set[str] = set()
        self._avoided_categories: set[str] = set()

    def set_preferred_categories(self, categories: set[str]) -> None:
        """Called by MetaStrategy when it finds profitable categories."""
        self._preferred_categories = categories
        log.info("market_filter_preferred_categories", categories=list(categories))

    def set_avoided_categories(self, categories: set[str]) -> None:
        """Called by MetaStrategy when it finds unprofitable categories."""
        self._avoided_categories = categories
        log.info("market_filter_avoided_categories", categories=list(categories))

    def filter(
        self,
        markets: list[MarketCandidate],
        held_market_ids: set[str] | None = None,
    ) -> list[MarketCandidate]:
        held = held_market_ids or set()
        filtered = []

        now = datetime.utcnow()

        for m in markets:
            # Skip markets we already hold
            if m.condition_id in held:
                continue

            # Volume check
            if m.volume < self._min_volume:
                continue

            # Spread check
            if m.spread > self._max_spread:
                continue

            # Time to resolution check
            try:
                end = datetime.fromisoformat(m.end_date.replace("Z", "+00:00"))
                time_left = end.replace(tzinfo=None) - now
                hours_left = time_left.total_seconds() / 3600
                days_left = time_left.days

                # Must resolve within max_days and not already expired
                if days_left > self._max_days or hours_left < 0:
                    continue
            except (ValueError, TypeError):
                continue

            # Skip extreme prices (already nearly resolved)
            if m.yes_price > 0.95 or m.yes_price < 0.05:
                continue

            # Category-based filtering from MetaStrategy insights
            if self._avoided_categories:
                category = classify_market_question(m.question)
                if category in self._avoided_categories:
                    log.debug("market_skipped_avoided_category", category=category, market=m.question[:50])
                    continue

            # Store hours_left and category for sorting
            m._hours_left = hours_left
            m._category = classify_market_question(m.question) if (self._preferred_categories or self._avoided_categories) else "unknown"
            filtered.append(m)

        # Sort: preferred categories first, then ultra-fast markets, then by volume.
        def sort_key(m):
            h = getattr(m, "_hours_left", 999)
            is_ultra_fast = h <= self._max_hours
            cat = getattr(m, "_category", "unknown")
            is_preferred = cat in self._preferred_categories
            # Preferred categories first (0 vs 1), then ultra-fast (0 vs 1), then volume desc
            return (0 if is_preferred else 1, 0 if is_ultra_fast else 1, -m.volume_24h)

        filtered.sort(key=sort_key)
        result = filtered[: self._top_n]

        ultra_fast_count = sum(1 for m in result if getattr(m, "_hours_left", 999) <= self._max_hours)
        preferred_count = sum(1 for m in result if getattr(m, "_category", "unknown") in self._preferred_categories)
        log.info(
            "markets_filtered",
            input=len(markets),
            output=len(result),
            ultra_fast=ultra_fast_count,
            preferred=preferred_count,
            avoided_categories=list(self._avoided_categories),
            max_hours=self._max_hours,
        )
        return result
