"""End-to-end tick tests with mocked dependencies.

The bot's tick function coordinates scrape → extract → analyze → trade. These
tests exercise the coordination logic without hitting any real network."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from wsb_trader.ai_client import TickerAnalysis
from wsb_trader.bot import TickIO, tick
from wsb_trader.config import Config
from wsb_trader.scraper import RedditPost
from wsb_trader.trader import Position


def _cfg(**overrides) -> Config:
    defaults = dict(
        alpaca_api_key="k", alpaca_api_secret="s",
        alpaca_base_url="https://paper-api.alpaca.markets",
        ai_base_url="https://ai/v1", ai_api_key="k", ai_model="m",
        reddit_user_agent="wsb-trader-test/0.1 by u/fin",
        reddit_client_id="test_id",
        reddit_client_secret="test_secret",
        enable_reddit=True, enable_4chan=True, enable_yahoo=True, enable_stocktwits=True,
        poll_interval_seconds=60,
        min_mentions=2,
        min_confidence=0.7,
        position_size_usd=Decimal("1000"),
        max_positions=5,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _post(pid: str, title: str, body: str = "") -> RedditPost:
    return RedditPost(
        id=pid, title=title, selftext=body, score=0, num_comments=0,
        permalink=f"/{pid}", flair=None,
    )


def _mock_io(
    posts: list[RedditPost],
    analyses: dict[str, TickerAnalysis],
    positions: list[Position] | None = None,
    cfg: Config | None = None,
) -> TickIO:
    scraper = MagicMock()
    scraper.fetch = AsyncMock(return_value=posts)

    ai = MagicMock()
    async def _analyze(ticker: str, excerpts: list[str]):
        return analyses.get(ticker, TickerAnalysis(ticker, "HOLD", 0.0, ""))
    ai.analyze = AsyncMock(side_effect=_analyze)

    trader = MagicMock()
    trader.list_positions = AsyncMock(return_value=positions or [])
    trader.place_market_order = AsyncMock()
    trader.close_position = AsyncMock()

    return TickIO(scrapers=[scraper], ai=ai, trader=trader, config=cfg or _cfg())


class TestTickHappyPath:
    async def test_buys_high_confidence_ticker_with_no_position(self):
        # AAPL mentioned 3 times (above min_mentions=2), high-conf BUY,
        # no existing position → we should buy.
        posts = [
            _post("1", "$AAPL to the moon"),
            _post("2", "$AAPL earnings play"),
            _post("3", "loading up on $AAPL"),
        ]
        analyses = {"AAPL": TickerAnalysis("AAPL", "BUY", 0.9, "bullish")}
        io = _mock_io(posts, analyses)

        await tick(io)

        io.trader.place_market_order.assert_awaited_once_with(
            "AAPL", notional=Decimal("1000"), side="buy",
        )
        io.trader.close_position.assert_not_awaited()

    async def test_closes_position_on_high_conf_sell(self):
        posts = [_post(str(i), "$GME crashing") for i in range(3)]
        analyses = {"GME": TickerAnalysis("GME", "SELL", 0.85, "bearish")}
        positions = [Position(
            symbol="GME", qty=Decimal("10"), market_value=Decimal("500"),
            unrealized_pl=Decimal("-100"), avg_entry_price=Decimal("60"),
        )]
        io = _mock_io(posts, analyses, positions)

        await tick(io)

        io.trader.close_position.assert_awaited_once_with("GME")
        io.trader.place_market_order.assert_not_awaited()


class TestTickThresholds:
    async def test_skips_ticker_below_mention_threshold(self):
        # AAPL only mentioned once, below min_mentions=2 → not analyzed, no trade.
        posts = [_post("1", "$AAPL only mentioned once")]
        io = _mock_io(posts, {"AAPL": TickerAnalysis("AAPL", "BUY", 0.9, "")})

        await tick(io)

        io.ai.analyze.assert_not_awaited()
        io.trader.place_market_order.assert_not_awaited()

    async def test_skips_low_confidence_buy(self):
        posts = [_post(str(i), "$AAPL") for i in range(3)]
        # Confidence 0.5 is below min_confidence=0.7.
        analyses = {"AAPL": TickerAnalysis("AAPL", "BUY", 0.5, "meh")}
        io = _mock_io(posts, analyses)

        await tick(io)

        io.trader.place_market_order.assert_not_awaited()

    async def test_skips_buy_when_already_holding(self):
        # Already have an AAPL position → don't double-up on BUY signal.
        posts = [_post(str(i), "$AAPL") for i in range(3)]
        analyses = {"AAPL": TickerAnalysis("AAPL", "BUY", 0.9, "")}
        positions = [Position(
            symbol="AAPL", qty=Decimal("5"), market_value=Decimal("875"),
            unrealized_pl=Decimal("0"), avg_entry_price=Decimal("175"),
        )]
        io = _mock_io(posts, analyses, positions)

        await tick(io)

        io.trader.place_market_order.assert_not_awaited()

    async def test_skips_sell_when_no_position(self):
        # SELL signal but we don't hold it → nothing to do.
        posts = [_post(str(i), "$AAPL crashing") for i in range(3)]
        analyses = {"AAPL": TickerAnalysis("AAPL", "SELL", 0.9, "")}
        io = _mock_io(posts, analyses)

        await tick(io)

        io.trader.close_position.assert_not_awaited()

    async def test_hold_signal_never_trades(self):
        posts = [_post(str(i), "$AAPL sideways") for i in range(3)]
        analyses = {"AAPL": TickerAnalysis("AAPL", "HOLD", 0.95, "")}
        io = _mock_io(posts, analyses)

        await tick(io)

        io.trader.place_market_order.assert_not_awaited()
        io.trader.close_position.assert_not_awaited()

    async def test_respects_max_positions(self):
        posts = [_post(str(i), "$NVDA") for i in range(3)]
        analyses = {"NVDA": TickerAnalysis("NVDA", "BUY", 0.95, "")}
        # Already at max_positions=5, so NVDA buy should be skipped.
        positions = [
            Position(symbol=s, qty=Decimal("1"), market_value=Decimal("100"),
                     unrealized_pl=Decimal("0"), avg_entry_price=Decimal("100"))
            for s in ("A", "B", "C", "D", "E")
        ]
        io = _mock_io(posts, analyses, positions, cfg=_cfg(max_positions=5))

        await tick(io)

        io.trader.place_market_order.assert_not_awaited()


class TestTickResilience:
    async def test_scrape_failure_short_circuits(self):
        io = _mock_io([], {})
        io.scrapers[0].fetch = AsyncMock(side_effect=RuntimeError("reddit down"))

        # Should not raise — errors get logged and swallowed.
        await tick(io)

        io.ai.analyze.assert_not_awaited()
        io.trader.list_positions.assert_not_awaited()

    async def test_ai_failure_for_one_ticker_does_not_block_others(self):
        posts = [
            _post("1", "$AAPL $NVDA"),
            _post("2", "$AAPL $NVDA"),
            _post("3", "$AAPL $NVDA"),
        ]
        io = _mock_io(posts, {})
        # AAPL errors, NVDA succeeds — we should still buy NVDA.
        async def _analyze(ticker: str, excerpts: list[str]):
            if ticker == "AAPL":
                raise RuntimeError("ai down for aapl")
            return TickerAnalysis(ticker, "BUY", 0.9, "")
        io.ai.analyze = AsyncMock(side_effect=_analyze)

        await tick(io)

        called_tickers = {c.args[0] for c in io.trader.place_market_order.await_args_list}
        assert called_tickers == {"NVDA"}

    async def test_position_list_failure_short_circuits_trading(self):
        posts = [_post(str(i), "$AAPL") for i in range(3)]
        analyses = {"AAPL": TickerAnalysis("AAPL", "BUY", 0.9, "")}
        io = _mock_io(posts, analyses)
        io.trader.list_positions = AsyncMock(side_effect=RuntimeError("alpaca down"))

        await tick(io)

        io.trader.place_market_order.assert_not_awaited()


class TestExcerptRouting:
    async def test_ai_gets_only_posts_that_mention_the_ticker(self):
        """When AAPL and NVDA both cross the threshold, the AAPL analysis
        should only include AAPL-mentioning posts (and vice versa)."""
        posts = [
            _post("1", "$AAPL earnings"),
            _post("2", "$AAPL calls printing"),
            _post("3", "$NVDA up big"),
            _post("4", "$NVDA to the moon"),
            _post("5", "$AAPL $NVDA both bullish"),
        ]
        io = _mock_io(posts, {
            "AAPL": TickerAnalysis("AAPL", "HOLD", 0.5, ""),
            "NVDA": TickerAnalysis("NVDA", "HOLD", 0.5, ""),
        })

        await tick(io)

        # Inspect what excerpts each ticker got.
        by_ticker = {call.args[0]: call.args[1] for call in io.ai.analyze.await_args_list}
        assert all("AAPL" in text for text in by_ticker["AAPL"])
        assert all("NVDA" in text for text in by_ticker["NVDA"])
        # Post 5 mentions both -> should appear in both lists.
        both_mention_post = next(t for t in by_ticker["AAPL"] if "NVDA" in t)
        assert both_mention_post in by_ticker["NVDA"]
