import time

import httpx
import pytest
import respx

from wsb_trader.scraper import (
    RedditPost,
    RedditScraper,
    TOKEN_URL,
    _parse_listing,
)


def _listing(posts: list[dict]) -> dict:
    """Wrap post dicts in Reddit's listing envelope."""
    return {
        "kind": "Listing",
        "data": {
            "children": [{"kind": "t3", "data": p} for p in posts],
        },
    }


def _make_scraper() -> RedditScraper:
    return RedditScraper(
        client_id="ci", client_secret="cs",
        user_agent="wsb-trader-tests/0.1 by u/fin",
    )


class TestParseListing:
    def test_parses_minimal_post(self):
        payload = _listing([{
            "id": "abc",
            "title": "YOLO on $AAPL",
            "selftext": "going all in",
            "score": 42,
            "num_comments": 7,
            "permalink": "/r/wsb/abc",
            "link_flair_text": "YOLO",
        }])
        posts = _parse_listing(payload)
        assert len(posts) == 1
        p = posts[0]
        assert p.id == "abc"
        assert p.title == "YOLO on $AAPL"
        assert p.selftext == "going all in"
        assert p.score == 42
        assert p.num_comments == 7
        assert p.flair == "YOLO"

    def test_handles_null_selftext(self):
        payload = _listing([{
            "id": "x", "title": "t", "selftext": None,
            "score": 0, "num_comments": 0, "permalink": "/x",
        }])
        posts = _parse_listing(payload)
        assert posts[0].selftext == ""

    def test_skips_non_post_children(self):
        payload = {
            "data": {
                "children": [
                    {"kind": "t3", "data": {"id": "a", "title": "post"}},
                    {"kind": "t1", "data": {"id": "b", "body": "comment"}},
                ]
            }
        }
        assert [p.id for p in _parse_listing(payload)] == ["a"]

    def test_empty_listing(self):
        assert _parse_listing({"data": {"children": []}}) == []

    def test_combined_text_property(self):
        p = RedditPost(
            id="a", title="Title", selftext="Body",
            score=1, num_comments=1, permalink="/a", flair=None,
        )
        assert p.combined_text == "Title\nBody"


class TestScraperInit:
    def test_rejects_missing_client_id(self):
        with pytest.raises(ValueError, match="REDDIT_CLIENT_ID"):
            RedditScraper(client_id="", client_secret="s", user_agent="ua/0.1 by u/x")

    def test_rejects_missing_client_secret(self):
        with pytest.raises(ValueError, match="REDDIT_CLIENT_ID"):
            RedditScraper(client_id="i", client_secret="", user_agent="ua/0.1 by u/x")

    def test_rejects_placeholder_user_agent(self):
        with pytest.raises(ValueError, match="REDDIT_USER_AGENT"):
            RedditScraper(
                client_id="i", client_secret="s",
                user_agent="wsb-trader/0.1 by u/yourhandle",
            )


class TestOAuthTokenFlow:
    @respx.mock
    async def test_fetch_hot_gets_token_first(self):
        scraper = _make_scraper()
        token_route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={
                "access_token": "tok-1", "expires_in": 3600, "token_type": "bearer",
            })
        )
        listing_route = respx.get("https://oauth.reddit.com/r/wallstreetbets/hot").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        await scraper.fetch_hot()
        await scraper.close()
        assert token_route.called
        assert listing_route.called
        # The listing request should carry a bearer of the token we got.
        auth_header = listing_route.calls.last.request.headers["authorization"]
        assert auth_header == "Bearer tok-1"

    @respx.mock
    async def test_token_is_cached_across_requests(self):
        scraper = _make_scraper()
        token_route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={
                "access_token": "tok-cached", "expires_in": 3600,
            })
        )
        listing_route = respx.get("https://oauth.reddit.com/r/wallstreetbets/hot").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        await scraper.fetch_hot()
        await scraper.fetch_hot()
        await scraper.fetch_hot()
        await scraper.close()
        # Token endpoint hit exactly once; three listing calls used the cache.
        assert token_route.call_count == 1
        assert listing_route.call_count == 3

    @respx.mock
    async def test_token_refresh_when_expired(self):
        scraper = _make_scraper()
        # Force the cache to look expired.
        scraper._token = "old"
        scraper._token_expires_at = time.time() - 1
        token_route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={
                "access_token": "fresh", "expires_in": 3600,
            })
        )
        listing_route = respx.get("https://oauth.reddit.com/r/wallstreetbets/hot").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        await scraper.fetch_hot()
        await scraper.close()
        assert token_route.called
        assert listing_route.calls.last.request.headers["authorization"] == "Bearer fresh"

    @respx.mock
    async def test_token_endpoint_gets_client_credentials_grant(self):
        scraper = _make_scraper()
        token_route = respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={
                "access_token": "t", "expires_in": 3600,
            })
        )
        respx.get("https://oauth.reddit.com/r/wallstreetbets/hot").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        await scraper.fetch_hot()
        await scraper.close()

        req = token_route.calls.last.request
        assert b"grant_type=client_credentials" in req.content
        # Basic auth header carries client_id:client_secret base64-encoded.
        assert req.headers["authorization"].startswith("Basic ")


class TestScraperFetching:
    @respx.mock
    async def test_fetch_hot_returns_parsed_posts(self):
        scraper = _make_scraper()
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        )
        respx.get("https://oauth.reddit.com/r/wallstreetbets/hot").mock(
            return_value=httpx.Response(200, json=_listing([
                {
                    "id": "1", "title": "$GME to the moon", "selftext": "",
                    "score": 100, "num_comments": 50, "permalink": "/1",
                    "link_flair_text": "DD",
                },
            ]))
        )
        posts = await scraper.fetch_hot()
        await scraper.close()
        assert len(posts) == 1
        assert posts[0].title == "$GME to the moon"

    @respx.mock
    async def test_fetch_hot_sends_limit_param(self):
        scraper = _make_scraper()
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        )
        listing_route = respx.get("https://oauth.reddit.com/r/wallstreetbets/hot").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        await scraper.fetch_hot(limit=25)
        await scraper.close()
        assert listing_route.calls.last.request.url.params.get("limit") == "25"

    @respx.mock
    async def test_raises_on_listing_http_error(self):
        scraper = _make_scraper()
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        )
        respx.get("https://oauth.reddit.com/r/wallstreetbets/hot").mock(
            return_value=httpx.Response(429)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await scraper.fetch_hot()
        await scraper.close()

    @respx.mock
    async def test_fetch_new_and_rising_use_different_paths(self):
        scraper = _make_scraper()
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        )
        new_route = respx.get("https://oauth.reddit.com/r/wallstreetbets/new").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        rising_route = respx.get("https://oauth.reddit.com/r/wallstreetbets/rising").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        await scraper.fetch_new()
        await scraper.fetch_rising()
        await scraper.close()
        assert new_route.called
        assert rising_route.called
