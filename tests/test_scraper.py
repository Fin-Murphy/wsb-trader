import httpx
import pytest
import respx

from wsb_trader.scraper import (
    API_BASE,
    TOKEN_URL,
    RedditPost,
    RedditScraper,
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


def _token_response(token: str = "test_token") -> httpx.Response:
    return httpx.Response(200, json={"access_token": token, "expires_in": 3600})


def _make_scraper() -> RedditScraper:
    return RedditScraper(
        user_agent="wsb-trader-tests/0.1 by u/fin",
        client_id="test_client_id",
        client_secret="test_client_secret",
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
    def test_rejects_empty_user_agent(self):
        with pytest.raises(ValueError, match="REDDIT_USER_AGENT"):
            RedditScraper(user_agent="", client_id="id", client_secret="secret")

    def test_rejects_placeholder_user_agent(self):
        with pytest.raises(ValueError, match="REDDIT_USER_AGENT"):
            RedditScraper(
                user_agent="wsb-trader/0.1 by u/yourhandle",
                client_id="id",
                client_secret="secret",
            )

    def test_rejects_missing_client_id(self):
        with pytest.raises(ValueError, match="REDDIT_CLIENT_ID"):
            RedditScraper(
                user_agent="wsb-trader/0.1 by u/fin",
                client_id="",
                client_secret="secret",
            )

    def test_rejects_missing_client_secret(self):
        with pytest.raises(ValueError, match="REDDIT_CLIENT_ID"):
            RedditScraper(
                user_agent="wsb-trader/0.1 by u/fin",
                client_id="id",
                client_secret="",
            )

    def test_sends_user_agent_header_on_owned_client(self):
        scraper = _make_scraper()
        assert scraper._client.headers["User-Agent"] == "wsb-trader-tests/0.1 by u/fin"


class TestScraperFetching:
    @respx.mock
    async def test_fetch_hot_returns_parsed_posts(self):
        scraper = _make_scraper()
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(f"{API_BASE}/r/wallstreetbets/hot").mock(
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
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        route = respx.get(f"{API_BASE}/r/wallstreetbets/hot").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        await scraper.fetch_hot(limit=25)
        await scraper.close()
        params = route.calls.last.request.url.params
        assert params.get("limit") == "25"

    @respx.mock
    async def test_sends_bearer_auth_header(self):
        scraper = _make_scraper()
        respx.post(TOKEN_URL).mock(return_value=_token_response("mytoken"))
        route = respx.get(f"{API_BASE}/r/wallstreetbets/hot").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        await scraper.fetch_hot()
        await scraper.close()
        assert route.calls.last.request.headers.get("authorization") == "bearer mytoken"

    @respx.mock
    async def test_token_is_cached_across_calls(self):
        scraper = _make_scraper()
        token_route = respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(f"{API_BASE}/r/wallstreetbets/hot").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        await scraper.fetch_hot()
        await scraper.fetch_hot()
        await scraper.close()
        assert token_route.call_count == 1  # fetched once, reused on second call

    @respx.mock
    async def test_raises_on_listing_http_error(self):
        scraper = _make_scraper()
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        respx.get(f"{API_BASE}/r/wallstreetbets/hot").mock(
            return_value=httpx.Response(429)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await scraper.fetch_hot()
        await scraper.close()

    @respx.mock
    async def test_raises_on_token_http_error(self):
        scraper = _make_scraper()
        respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))
        with pytest.raises(httpx.HTTPStatusError):
            await scraper.fetch_hot()
        await scraper.close()

    @respx.mock
    async def test_fetch_new_and_rising_use_different_paths(self):
        scraper = _make_scraper()
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        new_route = respx.get(f"{API_BASE}/r/wallstreetbets/new").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        rising_route = respx.get(f"{API_BASE}/r/wallstreetbets/rising").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        await scraper.fetch_new()
        await scraper.fetch_rising()
        await scraper.close()
        assert new_route.called
        assert rising_route.called

    @respx.mock
    async def test_custom_subreddit(self):
        scraper = RedditScraper(
            user_agent="wsb-trader-tests/0.1 by u/fin",
            client_id="test_client_id",
            client_secret="test_client_secret",
            subreddit="stocks",
        )
        respx.post(TOKEN_URL).mock(return_value=_token_response())
        route = respx.get(f"{API_BASE}/r/stocks/hot").mock(
            return_value=httpx.Response(200, json=_listing([]))
        )
        await scraper.fetch_hot()
        await scraper.close()
        assert route.called
