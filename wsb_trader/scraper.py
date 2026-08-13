"""Fetch recent posts from r/wallstreetbets via Reddit's OAuth API.

Reddit no longer serves ``www.reddit.com/*.json`` reliably to non-browser
clients — even with a distinctive User-Agent, requests get 403'd. So this
scraper uses the OAuth flow:

  1. Client-credentials grant to ``www.reddit.com/api/v1/access_token``.
  2. Cached bearer token (valid ~1h) used against ``oauth.reddit.com``.
  3. Refresh when the token nears expiry.

You need a Reddit "script" app to get a client_id and client_secret:
  https://www.reddit.com/prefs/apps -> "create app" -> type: script
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
# Refresh a bit before actual expiry to avoid using a token that dies mid-request.
TOKEN_REFRESH_MARGIN_SECONDS = 60


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
        client_id: str,
        client_secret: str,
        user_agent: str,
        subreddit: str = "wallstreetbets",
        client: httpx.AsyncClient | None = None,
    ):
        if not client_id or not client_secret:
            raise ValueError(
                "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set. "
                "Create a 'script' app at https://www.reddit.com/prefs/apps"
            )
        if not user_agent or "yourhandle" in user_agent.lower():
            raise ValueError(
                "REDDIT_USER_AGENT must be set to a distinctive string, "
                "e.g. 'wsb-trader/0.1 by u/yourhandle'"
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self.subreddit = subreddit
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=15.0,
        )
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_hot(self, limit: int = 50) -> list[RedditPost]:
        return await self._fetch_listing("hot", limit)

    async def fetch_new(self, limit: int = 50) -> list[RedditPost]:
        return await self._fetch_listing("new", limit)

    async def fetch_rising(self, limit: int = 50) -> list[RedditPost]:
        return await self._fetch_listing("rising", limit)

    async def _fetch_listing(self, sort: str, limit: int) -> list[RedditPost]:
        token = await self._get_token()
        url = f"{API_BASE}/r/{self.subreddit}/{sort}"
        resp = await self._client.get(
            url,
            params={"limit": limit, "raw_json": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return _parse_listing(resp.json())

    async def _get_token(self) -> str:
        """Return a cached token or fetch a new one."""
        if self._token and time.time() < self._token_expires_at - TOKEN_REFRESH_MARGIN_SECONDS:
            return self._token
        resp = await self._client.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires_at = time.time() + int(body.get("expires_in", 3600))
        return self._token


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
