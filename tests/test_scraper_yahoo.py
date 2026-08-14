import httpx
import pytest
import respx

from wsb_trader.scraper_yahoo import (
    NEWS_URL,
    TRENDING_URL,
    YahooFinanceScraper,
    _parse_news,
    _parse_trending,
)


def _trending_payload(symbols: list[str]) -> dict:
    return {
        "finance": {
            "result": [
                {"quotes": [{"symbol": s} for s in symbols]}
            ]
        }
    }


def _news_payload(items: list[dict]) -> dict:
    return {"news": items}


class TestParseTrending:
    def test_converts_symbols_to_cashtag_posts(self):
        payload = _trending_payload(["AAPL", "TSLA"])
        posts = _parse_trending(payload, top_n=10)
        assert len(posts) == 2
        aapl = next(p for p in posts if "AAPL" in p.text)
        assert "$AAPL" in aapl.text
        # Should appear multiple times so extractor registers a high count.
        assert aapl.text.count("$AAPL") >= 2

    def test_skips_dotted_tickers(self):
        payload = _trending_payload(["AAPL", "BRK.B", "TSLA"])
        posts = _parse_trending(payload, top_n=10)
        syms = {p.text.split()[0] for p in posts}
        assert "$BRK.B" not in " ".join(p.text for p in posts)

    def test_respects_top_n(self):
        payload = _trending_payload(["A", "B", "C", "D", "E"])
        posts = _parse_trending(payload, top_n=3)
        assert len(posts) == 3

    def test_tolerates_missing_result(self):
        assert _parse_trending({}, top_n=10) == []
        assert _parse_trending({"finance": {}}, top_n=10) == []
        assert _parse_trending({"finance": {"result": []}}, top_n=10) == []

    def test_combined_text_equals_text(self):
        payload = _trending_payload(["NVDA"])
        post = _parse_trending(payload, top_n=5)[0]
        assert post.combined_text == post.text


class TestParseNews:
    def test_parses_title_and_summary(self):
        payload = _news_payload([{"title": "AAPL up 3%", "summary": "Strong earnings beat."}])
        posts = _parse_news(payload)
        assert len(posts) == 1
        assert "AAPL up 3%" in posts[0].text
        assert "Strong earnings beat." in posts[0].text

    def test_handles_missing_summary(self):
        payload = _news_payload([{"title": "TSLA drops"}])
        posts = _parse_news(payload)
        assert posts[0].text == "TSLA drops"

    def test_skips_empty_title(self):
        payload = _news_payload([{"title": "", "summary": "no title"}])
        posts = _parse_news(payload)
        assert posts == []

    def test_empty_news_list(self):
        assert _parse_news({"news": []}) == []


class TestYahooFinanceScraper:
    @respx.mock
    async def test_fetch_combines_trending_and_news(self):
        respx.get(TRENDING_URL).mock(
            return_value=httpx.Response(200, json=_trending_payload(["AAPL"]))
        )
        respx.get(NEWS_URL).mock(
            return_value=httpx.Response(200, json=_news_payload([
                {"title": "Markets rally", "summary": "Stocks up."}
            ]))
        )
        scraper = YahooFinanceScraper()
        posts = await scraper.fetch()
        await scraper.close()
        sources = {p.source for p in posts}
        assert "yahoo_trending" in sources
        assert "yahoo_news" in sources

    @respx.mock
    async def test_fetch_raises_on_trending_http_error(self):
        respx.get(TRENDING_URL).mock(return_value=httpx.Response(429))
        respx.get(NEWS_URL).mock(
            return_value=httpx.Response(200, json=_news_payload([]))
        )
        scraper = YahooFinanceScraper()
        with pytest.raises(httpx.HTTPStatusError):
            await scraper.fetch()
        await scraper.close()

    @respx.mock
    async def test_fetch_raises_on_news_http_error(self):
        respx.get(TRENDING_URL).mock(
            return_value=httpx.Response(200, json=_trending_payload([]))
        )
        respx.get(NEWS_URL).mock(return_value=httpx.Response(500))
        scraper = YahooFinanceScraper()
        with pytest.raises(httpx.HTTPStatusError):
            await scraper.fetch()
        await scraper.close()
