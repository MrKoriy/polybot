from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NewsItem:
    title: str
    body: str
    source: str
    url: str
    published_at: datetime
    relevance_score: float | None = None

    def __str__(self) -> str:
        return f"[{self.source}] {self.title}"


@dataclass
class MarketCandidate:
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    volume: float
    volume_24h: float
    end_date: str
    spread: float = 0.0
    category: str = ""
    tags: list[str] = field(default_factory=list)


class NewsSource(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def fetch_news(self, query: str, lookback_hours: int = 2) -> list[NewsItem]: ...

    @abstractmethod
    async def health_check(self) -> bool: ...
