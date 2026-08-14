"""Fetch trending messages from StockTwits.

StockTwits exposes a public streaming API that doesn't require authentication
for read-only access. The rate limit for unauthenticated clients is 200
requests/hour, which our poll interval keeps us well under.

Reference: https://api.stocktwits.com/developers/docs/api
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx


TRENDING_URL = "https://api.stocktwits.com/api/2/streams/trending.json"

_HEADERS = {
    "User-Agent": "wsb-trader/0.1",
    "Accept": "application/json",
}


@dataclass(frozen=True)
class StockTwitsPost:
    id: int
    body: str
    sentiment: str | None  # "Bullish", "Bearish", or None

    @property
    def combined_text(self) -> str:
        if self.sentiment:
            return f"{self.body} [{self.sentiment}]"
        return self.body


class StockTwitsScraper:
    def __init__(self, *, client: httpx.AsyncClient | None = None):
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers=_HEADERS,
            timeout=15.0,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self) -> list[StockTwitsPost]:
        resp = await self._client.get(TRENDING_URL)
        resp.raise_for_status()
        return _parse_messages(resp.json())


def _parse_messages(data: dict) -> list[StockTwitsPost]:
    posts = []
    for msg in data.get("messages", []):
        body = msg.get("body", "").strip()
        if not body:
            continue
        sentiment = None
        entities = msg.get("entities") or {}
        raw_sentiment = entities.get("sentiment") or {}
        if isinstance(raw_sentiment, dict):
            sentiment = raw_sentiment.get("basic")  # "Bullish" or "Bearish"
        posts.append(StockTwitsPost(
            id=msg.get("id", 0),
            body=body,
            sentiment=sentiment,
        ))
    return posts
