"""Fetch trending tickers and news headlines from Yahoo Finance.

Uses Yahoo Finance's undocumented-but-stable query endpoints — no API key
needed. Two data sources are combined:

1. Trending tickers (query2 trending endpoint) — gives us which symbols are
   getting attention right now. Each trending symbol becomes a synthetic post
   with repeated cashtag mentions so the extractor picks it up as a strong
   signal.

2. Market news search (query1 search endpoint) — returns recent headlines that
   the extractor can mine for additional ticker mentions and context text for
   the AI sentiment call.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx


TRENDING_URL = "https://query2.finance.yahoo.com/v1/finance/trending/US"
NEWS_URL = "https://query1.finance.yahoo.com/v1/finance/search"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; wsb-trader/0.1)"
    ),
    "Accept": "application/json",
}


@dataclass(frozen=True)
class YahooPost:
    source: str
    text: str

    @property
    def combined_text(self) -> str:
        return self.text


class YahooFinanceScraper:
    def __init__(
        self,
        *,
        top_n_trending: int = 20,
        news_count: int = 20,
        client: httpx.AsyncClient | None = None,
    ):
        self.top_n_trending = top_n_trending
        self.news_count = news_count
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers=_HEADERS,
            timeout=15.0,
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(self) -> list[YahooPost]:
        posts: list[YahooPost] = []
        posts.extend(await self._fetch_trending())
        posts.extend(await self._fetch_news())
        return posts

    async def _fetch_trending(self) -> list[YahooPost]:
        resp = await self._client.get(TRENDING_URL)
        resp.raise_for_status()
        return _parse_trending(resp.json(), top_n=self.top_n_trending)

    async def _fetch_news(self) -> list[YahooPost]:
        resp = await self._client.get(
            NEWS_URL,
            params={
                "q": "stock market",
                "newsCount": self.news_count,
                "enableNavLinks": "false",
                "enableEnhancedTrivialQuery": "true",
            },
        )
        resp.raise_for_status()
        return _parse_news(resp.json())


def _parse_trending(data: dict, *, top_n: int) -> list[YahooPost]:
    """Convert trending ticker list into high-signal cashtag posts.

    Repeating the cashtag 3× gives the extractor a count > 1 for each symbol
    so they can cross the min_mentions threshold when combined across sources.
    """
    try:
        quotes = data["finance"]["result"][0]["quotes"]
    except (KeyError, IndexError, TypeError):
        return []

    posts = []
    for q in quotes[:top_n]:
        sym = q.get("symbol", "").strip()
        if not sym or "." in sym:  # skip non-US tickers like BRK.B
            continue
        # Three cashtag mentions = unambiguous signal for the extractor.
        text = f"${sym} ${sym} ${sym} trending on Yahoo Finance"
        posts.append(YahooPost(source="yahoo_trending", text=text))
    return posts


def _parse_news(data: dict) -> list[YahooPost]:
    posts = []
    for item in data.get("news", []):
        title = item.get("title", "").strip()
        if not title:
            continue
        summary = item.get("summary", "").strip()
        text = f"{title}\n{summary}" if summary else title
        posts.append(YahooPost(source="yahoo_news", text=text))
    return posts
