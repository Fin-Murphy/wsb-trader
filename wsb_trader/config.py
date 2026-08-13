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

    reddit_client_id: str
    reddit_client_secret: str
    reddit_user_agent: str

    poll_interval_seconds: int
    min_mentions: int
    min_confidence: float
    position_size_usd: Decimal
    max_positions: int


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

        reddit_client_id=_require("REDDIT_CLIENT_ID"),
        reddit_client_secret=_require("REDDIT_CLIENT_SECRET"),
        reddit_user_agent=_require("REDDIT_USER_AGENT"),

        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "300")),
        min_mentions=int(os.getenv("MIN_MENTIONS", "3")),
        min_confidence=float(os.getenv("MIN_CONFIDENCE", "0.75")),
        position_size_usd=Decimal(os.getenv("POSITION_SIZE_USD", "1000")),
        max_positions=int(os.getenv("MAX_POSITIONS", "5")),
    )


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"required env var {name} is not set")
    return value
