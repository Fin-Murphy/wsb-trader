"""Load and validate runtime config from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    alpaca_api_key: str
    alpaca_api_secret: str
    alpaca_base_url: str

    ai_base_url: str
    ai_api_key: str
    ai_model: str

    reddit_user_agent: str
    reddit_client_id: str
    reddit_client_secret: str

    poll_interval_seconds: int
    min_mentions: int
    min_confidence: float
    position_size_usd: Decimal
    max_positions: int

    # Source toggles — set to "false" to disable a data source.
    enable_reddit: bool
    enable_4chan: bool
    enable_yahoo: bool
    enable_stocktwits: bool


def load_config(env_file: str | None = ".env") -> Config:
    """Load config, reading ``.env`` first if present."""
    if env_file:
        load_dotenv(env_file, override=False)

    return Config(
        alpaca_api_key=_require("ALPACA_API_KEY"),
        alpaca_api_secret=_require("ALPACA_API_SECRET"),
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),

        ai_base_url=_require("AI_BASE_URL"),
        ai_api_key=_require("AI_API_KEY"),
        ai_model=_require("AI_MODEL"),

        reddit_user_agent=_require("REDDIT_USER_AGENT"),
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID", ""),
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),

        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "300")),
        min_mentions=int(os.getenv("MIN_MENTIONS", "3")),
        min_confidence=float(os.getenv("MIN_CONFIDENCE", "0.75")),
        position_size_usd=Decimal(os.getenv("POSITION_SIZE_USD", "1000")),
        max_positions=int(os.getenv("MAX_POSITIONS", "5")),

        enable_reddit=os.getenv("ENABLE_REDDIT", "true").lower() not in ("0", "false", "no"),
        enable_4chan=os.getenv("ENABLE_4CHAN", "true").lower() not in ("0", "false", "no"),
        enable_yahoo=os.getenv("ENABLE_YAHOO", "true").lower() not in ("0", "false", "no"),
        enable_stocktwits=os.getenv("ENABLE_STOCKTWITS", "true").lower() not in ("0", "false", "no"),
    )


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"required env var {name} is not set")
    return value
