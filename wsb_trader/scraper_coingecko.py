"""Fetch trending crypto tickers from CoinGecko.

CoinGecko's public API is free and doesn't require an API key. Two endpoints
are combined:

1. Trending search (``/search/trending``) — the top 7 coins users have
   searched most in the last 24 hours. Each becomes a synthetic post with
   repeated cashtag mentions so the extractor picks it up as a strong signal.

2. Top gainers/losers via markets (``/coins/markets``) — surface coins with
   large 24h price swings so momentum plays get captured.

Reference: https://www.coingecko.com/en/api/documentation
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx


TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"
MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"

_HEADERS = {
    "User-Agent": "wsb-trader/0.1",
    "Accept": "application/json",
}


@dataclass(frozen=True)
class CoinGeckoPost:
    source: str
    text: str

    @property
    def combined_text(self) -> str:
        return self.text


class CoinGeckoScraper:
    def __init__(
        self,
        *,
        top_movers: int = 15,
        client: httpx.AsyncClient | None = None,
    ):
        self.top_movers = top_movers
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers=_HEADERS,
            timeout=15.0,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self) -> list[CoinGeckoPost]:
        posts: list[CoinGeckoPost] = []
        posts.extend(await self._fetch_trending())
        posts.extend(await self._fetch_movers())
        return posts

    async def _fetch_trending(self) -> list[CoinGeckoPost]:
        try:
            resp = await self._client.get(TRENDING_URL)
            resp.raise_for_status()
            return _parse_trending(resp.json())
        except httpx.HTTPError:
            return []

    async def _fetch_movers(self) -> list[CoinGeckoPost]:
        try:
            resp = await self._client.get(
                MARKETS_URL,
                params={
                    "vs_currency": "usd",
                    "order": "price_change_percentage_24h_desc",
                    "per_page": self.top_movers,
                    "page": 1,
                    "price_change_percentage": "24h",
                },
            )
            resp.raise_for_status()
            return _parse_movers(resp.json())
        except httpx.HTTPError:
            return []


def _parse_trending(data: dict) -> list[CoinGeckoPost]:
    """Convert CoinGecko's trending coins into cashtag-heavy posts.

    Repeating the cashtag 3× ensures the extractor's mention count exceeds
    the min_mentions threshold on a single tick.
    """
    posts = []
    for entry in data.get("coins", []):
        item = entry.get("item") or {}
        symbol = (item.get("symbol") or "").strip().upper()
        name = (item.get("name") or "").strip()
        if not symbol or len(symbol) > 5:
            continue
        text = f"${symbol} ${symbol} ${symbol} {name} trending on CoinGecko"
        posts.append(CoinGeckoPost(source="coingecko_trending", text=text))
    return posts


def _parse_movers(data: list) -> list[CoinGeckoPost]:
    """Convert top gainers/losers into posts with a bullish/bearish signal."""
    posts = []
    for coin in data:
        symbol = (coin.get("symbol") or "").strip().upper()
        name = (coin.get("name") or "").strip()
        change = coin.get("price_change_percentage_24h_in_currency")
        if not symbol or len(symbol) > 5 or change is None:
            continue
        direction = "surging" if change >= 0 else "crashing"
        text = f"${symbol} {name} {direction} {change:+.1f}% in 24h"
        posts.append(CoinGeckoPost(source="coingecko_movers", text=text))
    return posts
