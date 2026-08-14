import httpx
import pytest
import respx

from wsb_trader.scraper_4chan import (
    API_BASE,
    BizThread,
    FourchanScraper,
    _parse_catalog,
    _strip_html,
)


def _catalog(threads: list[dict]) -> list[dict]:
    return [{"page": 1, "threads": threads}]


def _thread(**kwargs) -> dict:
    defaults = {
        "no": 12345,
        "sub": "GME Thread",
        "com": "Going to the moon",
        "replies": 10,
    }
    defaults.update(kwargs)
    return defaults


class TestStripHtml:
    def test_strips_br_tags(self):
        assert _strip_html("line1<br>line2") == "line1 line2"

    def test_unescapes_entities(self):
        assert _strip_html("AT&amp;T") == "AT&T"

    def test_strips_anchor_tags(self):
        result = _strip_html('<a href="/biz/thread/1">link</a>')
        assert "<a" not in result
        assert "link" in result

    def test_empty_string(self):
        assert _strip_html("") == ""


class TestParseCatalog:
    def test_parses_basic_thread(self):
        catalog = _catalog([_thread(no=1, sub="$AAPL thread", com="buying calls")])
        threads = _parse_catalog(catalog, min_replies=5)
        assert len(threads) == 1
        t = threads[0]
        assert t.no == 1
        assert t.subject == "$AAPL thread"
        assert t.comment == "buying calls"

    def test_filters_low_reply_threads(self):
        catalog = _catalog([
            _thread(no=1, replies=10),
            _thread(no=2, replies=2),
        ])
        threads = _parse_catalog(catalog, min_replies=5)
        assert [t.no for t in threads] == [1]

    def test_skips_threads_with_no_text(self):
        catalog = _catalog([_thread(no=1, sub="", com="")])
        threads = _parse_catalog(catalog, min_replies=0)
        assert threads == []

    def test_handles_missing_sub(self):
        catalog = _catalog([{"no": 1, "com": "Just a comment", "replies": 10}])
        threads = _parse_catalog(catalog, min_replies=5)
        assert len(threads) == 1
        assert threads[0].subject == ""
        assert threads[0].comment == "Just a comment"

    def test_handles_multiple_pages(self):
        catalog = [
            {"page": 1, "threads": [_thread(no=1, replies=10)]},
            {"page": 2, "threads": [_thread(no=2, replies=10)]},
        ]
        threads = _parse_catalog(catalog, min_replies=5)
        assert {t.no for t in threads} == {1, 2}

    def test_strips_html_in_comment(self):
        catalog = _catalog([_thread(no=1, com="$GME<br>going up &amp; up", replies=10)])
        threads = _parse_catalog(catalog, min_replies=5)
        assert "<br>" not in threads[0].comment
        assert "&amp;" not in threads[0].comment
        assert "going up" in threads[0].comment

    def test_combined_text_joins_subject_and_comment(self):
        t = BizThread(no=1, subject="Sub", comment="Body")
        assert t.combined_text == "Sub\nBody"

    def test_empty_catalog(self):
        assert _parse_catalog([], min_replies=0) == []


class TestFourchanScraper:
    @respx.mock
    async def test_fetch_returns_parsed_threads(self):
        catalog = _catalog([_thread(no=99, sub="NVDA DD", com="huge catalyst", replies=20)])
        respx.get(f"{API_BASE}/biz/catalog.json").mock(
            return_value=httpx.Response(200, json=catalog)
        )
        scraper = FourchanScraper()
        threads = await scraper.fetch()
        await scraper.close()
        assert len(threads) == 1
        assert threads[0].subject == "NVDA DD"

    @respx.mock
    async def test_fetch_raises_on_http_error(self):
        respx.get(f"{API_BASE}/biz/catalog.json").mock(
            return_value=httpx.Response(503)
        )
        scraper = FourchanScraper()
        with pytest.raises(httpx.HTTPStatusError):
            await scraper.fetch()
        await scraper.close()

    @respx.mock
    async def test_custom_min_replies_filters_threads(self):
        catalog = _catalog([
            _thread(no=1, replies=50),
            _thread(no=2, replies=3),
        ])
        respx.get(f"{API_BASE}/biz/catalog.json").mock(
            return_value=httpx.Response(200, json=catalog)
        )
        scraper = FourchanScraper(min_replies=10)
        threads = await scraper.fetch()
        await scraper.close()
        assert [t.no for t in threads] == [1]

    @respx.mock
    async def test_custom_board(self):
        route = respx.get(f"{API_BASE}/smg/catalog.json").mock(
            return_value=httpx.Response(200, json=[])
        )
        scraper = FourchanScraper(board="smg")
        await scraper.fetch()
        await scraper.close()
        assert route.called
