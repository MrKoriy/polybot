from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("kelly_fraction", "max_position_pct")
    @classmethod
    def validate_fraction(cls, v: float) -> float:
        if not 0 < v <= 1.0:
            raise ValueError(f"Fraction must be in (0, 1.0], got {v}")
        return v

    @field_validator(
        "min_edge",
        "min_confidence",
        "llm_bias_correction",
        "daily_loss_limit",
        "drawdown_pause",
    )
    @classmethod
    def validate_positive_fraction(cls, v: float) -> float:
        if v < 0 or v > 1.0:
            raise ValueError(f"Value must be in [0, 1.0], got {v}")
        return v

    # Mode
    paper_trading: bool = True
    realistic_paper_execution: bool = True
    paper_bankroll: float = 100.0

    # Strategy gates.  The prior directional strategies are opt-in because the
    # repository's own research report found negative realised expectancy.
    enable_complete_set_shadow: bool = True
    enable_spread_trader: bool = False
    enable_threshold_trader: bool = False
    enable_weather_trader: bool = False
    enable_autoresearch: bool = False

    # Polymarket
    polymarket_private_key: str = ""
    polymarket_chain_id: int = 137
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_clob_proxy: str = ""  # SOCKS5 proxy for CLOB (EU IP for non-KYC live trading)
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    # Signature type:
    #   0 = EOA (direct) — default, works if Phantom private key directly controls balance
    #   1 = POLY_PROXY — Polymarket's proxy-wallet wrapping EOA (most web-connected accounts)
    #   2 = POLY_GNOSIS_SAFE — Gnosis Safe proxy (older web accounts)
    # If live orders reject with "funder mismatch" or similar, try 1 or 2.
    polymarket_signature_type: int = 0
    polymarket_funder_address: str = ""  # optional: if signature_type=1/2, the proxy address

    # LLM providers
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openrouter_api_key: str = ""

    # LLM models
    primary_llm_model: str = "google/gemini-3.1-flash-lite-preview"
    secondary_llm_model: str = "google/gemini-3.1-flash-lite-preview"
    primary_llm_provider: str = "openrouter"  # openrouter, claude, openai
    secondary_llm_provider: str = "openrouter"

    # Data sources
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "polymarket-bot/0.1"
    finnhub_api_key: str = ""
    brave_api_key: str = ""
    twitter_bearer_token: str = ""
    newsapi_key: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_polling_enabled: bool = False

    # Risk parameters — conservative $100 pilot profile
    kelly_fraction: float = 0.10
    max_position_pct: float = 0.10
    min_edge: float = 0.04
    min_confidence: float = 0.70
    min_market_volume: float = 1000.0       # sports markets can have lower volume
    max_open_positions_small: int = 5       # more positions since they close fast
    max_open_positions_large: int = 8
    daily_loss_limit: float = 0.015         # stop after -$1.50 on a $100 day start
    drawdown_pause: float = 0.06            # manual review after 6% peak drawdown
    llm_bias_correction: float = 0.04
    max_days_to_resolution: int = 3         # only markets resolving within 3 days
    max_hours_to_resolution: int = 48       # prefer markets resolving within 48 hours

    # Dashboard auth (basic auth — set in .env)
    dashboard_user: str = "admin"
    dashboard_password: str = ""  # empty = auth disabled

    # Operational
    scan_interval_seconds: int = 180        # 3 minutes — faster for short-term markets
    top_markets_to_analyze: int = 20
    news_lookback_hours: int = 12
    log_level: str = "INFO"
    db_path: str = "data/trades.db"
    breakers_state_file: str = "data/breakers_state.json"

    # Complete-set shadow maker.  Five shares is the current CLOB minimum on
    # most books; no more than half the bankroll may be reserved at once.
    maker_quote_shares: float = 5.0
    maker_min_gross_edge: float = 0.006
    maker_max_emergency_loss_usd: float = 0.15
    maker_max_market_reserve_pct: float = 0.10
    maker_max_total_reserve_pct: float = 0.50
    maker_max_markets: int = 8
    maker_max_resolution_hours: float = 72.0
    maker_min_volume_24h: float = 2_000.0
    maker_event_pages: int = 2
    maker_scan_interval_seconds: int = 60
    maker_request_timeout_seconds: float = 8.0
    maker_max_candidates_per_scan: int = 60

    @property
    def max_open_positions(self) -> int:
        return self.max_open_positions_large

    def get_kelly_fraction(self, bankroll: float) -> float:
        # Do not scale leverage up merely because a small account crossed an
        # arbitrary balance threshold.
        return self.kelly_fraction


settings = Settings()
