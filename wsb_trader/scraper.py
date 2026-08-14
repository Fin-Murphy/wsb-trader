"""Fetch recent posts from r/wallstreetbets via Reddit's OAuth API.

Uses the client-credentials grant (script-type app) so no user login is
needed. Register a "script" app at https://www.reddit.com/prefs/apps to
obtain REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET. The access token is
cached for its lifetime (1 hour) and refreshed automatically.

Rate limit for authenticated clients is 100 req/min, far above our needs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"

_TOKEN_REFRESH_BUFFER = 60  # refresh this many seconds before expiry


@dataclass(frozen=True)
class RedditPost:
    id: str
    title: str
    selftext: str
    score: int
    num_comments: int
    permalink: str
    flair: str | None

    @property
    def combined_text(self) -> str:
        """Title + body — the surface we run ticker extraction against."""
        return f"{self.title}\n{self.selftext}"


class RedditScraper:
    def __init__(
        self,
        *,
        user_agent: str,
        client_id: str,
        client_secret: str,
        subreddit: str = "wallstreetbets",
        client: httpx.AsyncClient | None = None,
    ):
        if not user_agent or "yourhandle" in user_agent.lower():
            raise ValueError(
                "REDDIT_USER_AGENT must be set to a distinctive string, "
                "e.g. 'wsb-trader/0.1 by u/yourhandle'"
            )
        if not client_id or not client_secret:
            raise ValueError(
                "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set. "
                "Register a 'script' app at https://www.reddit.com/prefs/apps"
            )
        self.user_agent = user_agent
        self.subreddit = subreddit
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=15.0,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _ensure_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at - _TOKEN_REFRESH_BUFFER:
            return self._token
        resp = await self._client.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.monotonic() + data.get("expires_in", 3600)
        return self._token

    async def fetch(self, limit: int = 50) -> list[RedditPost]:
        """Common interface used by the multi-source bot loop."""
        return await self.fetch_hot(limit)

    async def fetch_hot(self, limit: int = 50) -> list[RedditPost]:
        return await self._fetch_listing("hot", limit)

    async def fetch_new(self, limit: int = 50) -> list[RedditPost]:
        return await self._fetch_listing("new", limit)

    async def fetch_rising(self, limit: int = 50) -> list[RedditPost]:
        return await self._fetch_listing("rising", limit)

    async def _fetch_listing(self, sort: str, limit: int) -> list[RedditPost]:
        token = await self._ensure_token()
        url = f"{API_BASE}/r/{self.subreddit}/{sort}"
        resp = await self._client.get(
            url,
            params={"limit": limit},
            headers={"Authorization": f"bearer {token}"},
        )
        resp.raise_for_status()
        return _parse_listing(resp.json())


def _parse_listing(payload: dict) -> list[RedditPost]:
    """Turn Reddit's listing JSON into ``RedditPost`` objects.

    Reddit's response shape:
        {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": {...}}, ...]}}
    """
    posts: list[RedditPost] = []
    children = payload.get("data", {}).get("children", [])
    for child in children:
        if child.get("kind") != "t3":
            continue  # not a post (could be a comment or wiki entry)
        d = child.get("data", {})
        posts.append(RedditPost(
            id=d.get("id", ""),
            title=d.get("title", ""),
            selftext=d.get("selftext", "") or "",
            score=int(d.get("score", 0)),
            num_comments=int(d.get("num_comments", 0)),
            permalink=d.get("permalink", ""),
            flair=d.get("link_flair_text"),
        ))
    return posts
