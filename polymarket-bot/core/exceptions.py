class BotError(Exception):
    """Base exception for the trading bot."""


class ConfigError(BotError):
    """Invalid or missing configuration."""


class DataSourceError(BotError):
    """Error fetching data from an external source."""


class PolymarketAPIError(DataSourceError):
    """Error from Polymarket API."""


class LLMError(BotError):
    """Error from LLM provider."""


class InsufficientBalanceError(BotError):
    """Not enough USDC to place order."""


class RiskLimitError(BotError):
    """Trade rejected by risk manager."""


class OrderExecutionError(BotError):
    """Error placing or managing an order."""


class TelegramError(BotError):
    """Error sending Telegram notification."""
