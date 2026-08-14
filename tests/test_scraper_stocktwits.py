import httpx
import pytest
import respx

from wsb_trader.scraper_stocktwits import (
    TRENDING_URL,
    StockTwitsPost,
    StockTwitsScraper,
    _parse_messages,
)


def _payload(messages: list[dict]) -> dict:
    return {"response": {"status": 200}, "messages": messages}


def _msg(body: str, sentiment: str | None = None, mid: int = 1) -> dict:
    m: dict = {"id": mid, "body": body}
    if sentiment:
        m["entities"] = {"sentiment": {"basic": sentiment}}
    else:
        m["entities"] = {}
    return m


class TestParseMessages:
    def test_parses_basic_message(self):
        payload = _payload([_msg("$AAPL is going up", mid=42)])
        posts = _parse_messages(payload)
        assert len(posts) == 1
        assert posts[0].id == 42
        assert posts[0].body == "$AAPL is going up"
        assert posts[0].sentiment is None

    def test_parses_bullish_sentiment(self):
        posts = _parse_messages(_payload([_msg("$TSLA 🚀", sentiment="Bullish")]))
        assert posts[0].sentiment == "Bullish"

    def test_parses_bearish_sentiment(self):
        posts = _parse_messages(_payload([_msg("$GME dump", sentiment="Bearish")]))
        assert posts[0].sentiment == "Bearish"

    def test_combined_text_includes_sentiment_tag(self):
        post = StockTwitsPost(id=1, body="$AAPL up", sentiment="Bullish")
        assert post.combined_text == "$AAPL up [Bullish]"

    def test_combined_text_no_sentiment(self):
        post = StockTwitsPost(id=1, body="$AAPL up", sentiment=None)
        assert post.combined_text == "$AAPL up"

    def test_skips_empty_body(self):
        posts = _parse_messages(_payload([_msg(""), _msg("$NVDA puts")]))
        assert len(posts) == 1
        assert posts[0].body == "$NVDA puts"

    def test_empty_message_list(self):
        assert _parse_messages({"messages": []}) == []

    def test_tolerates_missing_entities(self):
        payload = _payload([{"id": 1, "body": "$COIN moon"}])
        posts = _parse_messages(payload)
        assert len(posts) == 1
        assert posts[0].sentiment is None


class TestStockTwitsScraper:
    @respx.mock
    async def test_fetch_returns_parsed_messages(self):
        respx.get(TRENDING_URL).mock(
            return_value=httpx.Response(200, json=_payload([
                _msg("$AAPL strong buy", sentiment="Bullish", mid=100),
            ]))
        )
        scraper = StockTwitsScraper()
        posts = await scraper.fetch()
        await scraper.close()
        assert len(posts) == 1
        assert posts[0].id == 100

    @respx.mock
    async def test_fetch_raises_on_http_error(self):
        respx.get(TRENDING_URL).mock(return_value=httpx.Response(429))
        scraper = StockTwitsScraper()
        with pytest.raises(httpx.HTTPStatusError):
            await scraper.fetch()
        await scraper.close()
