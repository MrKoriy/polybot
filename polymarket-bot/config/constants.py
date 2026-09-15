from enum import Enum


# Polymarket API URLs
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"
WS_LIVE_URL = "wss://ws-live-data.polymarket.com"

# Polygon chain
POLYGON_CHAIN_ID = 137


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Direction(str, Enum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"


class TradeStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class RiskEventType(str, Enum):
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    DRAWDOWN_PAUSE = "drawdown_pause"
    MAX_POSITIONS = "max_positions"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    LOW_LIQUIDITY = "low_liquidity"
    EDGE_TOO_SMALL = "edge_too_small"
    LOW_CONFIDENCE = "low_confidence"


class LLMProvider(str, Enum):
    CLAUDE = "claude"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
