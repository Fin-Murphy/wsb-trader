"""One iteration of the trading loop, wired end-to-end.

Kept as a standalone function so we can unit-test the whole flow with mocked
dependencies without spinning up the scheduler.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

from wsb_trader.ai_client import AIClient, AIResponseError, TickerAnalysis
from wsb_trader.config import Config
from wsb_trader.extractor import aggregate, extract
from wsb_trader.trader import PaperTrader

log = structlog.get_logger()


@dataclass
class TickIO:
    """Wired dependencies for one loop iteration. Injectable for testing."""
    scrapers: list[Any]  # each element must have async fetch() -> list[...with combined_text]
    ai: AIClient
    trader: PaperTrader
    config: Config


async def tick(io: TickIO) -> None:
    """Run one scrape → analyze → trade cycle.

    Scrapes all configured sources in parallel. A failure in one source is
    logged and skipped; the loop continues with whatever posts were gathered.
    """
    scrape_results = await asyncio.gather(
        *(_safe_fetch(s) for s in io.scrapers),
    )
    posts = [p for batch in scrape_results for p in batch]
    if not posts:
        log.info("no_posts_from_any_source")
        return
    log.info("scraped", n_posts=len(posts))

    mentions = aggregate([extract(p.combined_text) for p in posts])
    candidates = [m for m in mentions if m.count >= io.config.min_mentions]
    log.info(
        "tickers_extracted",
        total=len(mentions),
        above_threshold=len(candidates),
        threshold=io.config.min_mentions,
    )
    if not candidates:
        return

    # Build a per-ticker excerpt list: for each candidate ticker, gather the
    # posts that actually mentioned it (checked via a fresh extraction).
    per_ticker_excerpts: dict[str, list[str]] = {m.ticker: [] for m in candidates}
    for post in posts:
        text = post.combined_text
        tickers_in_post = {m.ticker for m in extract(text)}
        for candidate in candidates:
            if candidate.ticker in tickers_in_post:
                per_ticker_excerpts[candidate.ticker].append(text)

    # Analyze in parallel — each ticker is an independent LLM call.
    analyses = await asyncio.gather(*(
        _safe_analyze(io.ai, m.ticker, per_ticker_excerpts[m.ticker])
        for m in candidates
    ))

    # Execute trades based on signals.
    try:
        current_positions = {p.symbol: p for p in await io.trader.list_positions()}
    except Exception:
        log.exception("list_positions_failed")
        return

    for analysis in analyses:
        if analysis is None:
            continue
        await _execute_signal(io, analysis, current_positions)


async def _safe_fetch(scraper: Any) -> list[Any]:
    try:
        return await scraper.fetch()
    except Exception:
        log.exception("scrape_failed", source=type(scraper).__name__)
        return []


async def _safe_analyze(
    ai: AIClient, ticker: str, excerpts: list[str],
) -> TickerAnalysis | None:
    try:
        return await ai.analyze(ticker, excerpts)
    except AIResponseError:
        log.warning("ai_response_unparseable", ticker=ticker)
    except Exception:
        log.exception("ai_call_failed", ticker=ticker)
    return None


async def _execute_signal(
    io: TickIO,
    analysis: TickerAnalysis,
    current_positions: dict[str, object],
) -> None:
    has_position = analysis.ticker in current_positions
    log.info(
        "signal",
        ticker=analysis.ticker,
        signal=analysis.signal,
        confidence=analysis.confidence,
        has_position=has_position,
    )

    if analysis.confidence < io.config.min_confidence:
        return  # not confident enough to act

    if analysis.signal == "BUY" and not has_position:
        if len(current_positions) >= io.config.max_positions:
            log.info("at_max_positions_skipping_buy", ticker=analysis.ticker)
            return
        try:
            order = await io.trader.place_market_order(
                analysis.ticker,
                notional=io.config.position_size_usd,
                side="buy",
            )
            log.info("bought", ticker=analysis.ticker, order_id=order.id)
        except Exception:
            log.exception("buy_failed", ticker=analysis.ticker)

    elif analysis.signal == "SELL" and has_position:
        try:
            order = await io.trader.close_position(analysis.ticker)
            log.info("closed", ticker=analysis.ticker, order_id=order.id if order else None)
        except Exception:
            log.exception("close_failed", ticker=analysis.ticker)
