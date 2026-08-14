"""One iteration of the trading loop, wired end-to-end."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from wsb_trader.ai_client import AIClient, AIResponseError, TickerAnalysis
from wsb_trader.config import Config
from wsb_trader.extractor import aggregate, extract
from wsb_trader.state import DashboardState
from wsb_trader.trader import AssetNotTradable, PaperTrader

log = structlog.get_logger()


@dataclass
class TickIO:
    """Wired dependencies for one loop iteration. Injectable for testing."""
    scrapers: list[Any]  # each element must have async fetch() -> list[...with combined_text]
    ai: AIClient
    trader: PaperTrader
    config: Config
    seen_ids: set[str] = field(default_factory=set)
    state: DashboardState | None = None


async def tick(io: TickIO) -> None:
    scrape_results = await asyncio.gather(
        *(_safe_fetch(s) for s in io.scrapers),
    )
    all_posts = [p for batch in scrape_results for p in batch]

    posts = []
    for post in all_posts:
        pid = _post_id(post)
        if pid is not None and pid in io.seen_ids:
            continue
        posts.append(post)
        if pid is not None:
            io.seen_ids.add(pid)

    n_dupes = len(all_posts) - len(posts)

    if not posts:
        log.info("no_new_posts")
        return

    log.info("scraped", n_posts=len(posts), seen=n_dupes)
    if io.state is not None:
        io.state.last_tick_at = datetime.now(timezone.utc).isoformat()
        io.state.emit(
            "scrape",
            n_posts=len(posts),
            n_dupes=n_dupes,
            sources=[type(s).__name__ for s in io.scrapers],
        )

    mentions = aggregate([extract(p.combined_text) for p in posts])
    candidates = [m for m in mentions if m.count >= io.config.min_mentions]
    log.info(
        "tickers_extracted",
        total=len(mentions),
        above_threshold=len(candidates),
        threshold=io.config.min_mentions,
    )

    if io.state is not None:
        io.state.mentions = mentions
        io.state.emit(
            "mentions",
            tickers=[
                {"ticker": m.ticker, "count": m.count, "cashtag_count": m.cashtag_count}
                for m in mentions[:50]
            ],
        )

    if not candidates:
        return

    per_ticker_excerpts: dict[str, list[str]] = {m.ticker: [] for m in candidates}
    for post in posts:
        text = post.combined_text
        tickers_in_post = {m.ticker for m in extract(text)}
        for candidate in candidates:
            if candidate.ticker in tickers_in_post:
                per_ticker_excerpts[candidate.ticker].append(text)

    analyses = await asyncio.gather(*(
        _safe_analyze(io.ai, m.ticker, per_ticker_excerpts[m.ticker])
        for m in candidates
    ))

    try:
        current_positions = {p.symbol: p for p in await io.trader.list_positions()}
    except Exception:
        log.exception("list_positions_failed")
        return

    for analysis in analyses:
        if analysis is None:
            continue
        await _execute_signal(io, analysis, current_positions)

    # Refresh account + positions after all trades and publish to dashboard.
    if io.state is not None:
        try:
            refreshed = await io.trader.list_positions()
            acct = await io.trader.get_account()
            io.state.positions = refreshed
            io.state.account = acct
            io.state.emit(
                "positions",
                account={
                    "cash": str(acct.cash),
                    "buying_power": str(acct.buying_power),
                    "portfolio_value": str(acct.portfolio_value),
                    "equity": str(acct.equity),
                },
                positions=[
                    {
                        "symbol": p.symbol,
                        "qty": str(p.qty),
                        "market_value": str(p.market_value),
                        "unrealized_pl": str(p.unrealized_pl),
                        "avg_entry_price": str(p.avg_entry_price),
                    }
                    for p in refreshed
                ],
            )
        except Exception:
            log.exception("dashboard_refresh_failed")


def _post_id(post: Any) -> str | None:
    pid = getattr(post, "id", None)
    if pid is None:
        pid = getattr(post, "no", None)  # 4chan threads use .no
    if pid is None:
        return None
    return f"{type(post).__name__}:{pid}"


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

    if io.state is not None:
        io.state.emit(
            "signal",
            ticker=analysis.ticker,
            signal=analysis.signal,
            confidence=analysis.confidence,
            reasoning=analysis.reasoning,
        )

    if analysis.confidence < io.config.min_confidence:
        return

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
            if io.state is not None:
                io.state.emit(
                    "trade",
                    action="buy",
                    ticker=analysis.ticker,
                    notional=str(io.config.position_size_usd),
                    order_id=order.id,
                )
        except AssetNotTradable:
            log.warning("asset_not_tradable", ticker=analysis.ticker)
            if io.state is not None:
                io.state.emit("error", source="trade", message=f"{analysis.ticker} not tradable on Alpaca")
        except Exception:
            log.exception("buy_failed", ticker=analysis.ticker)
            if io.state is not None:
                io.state.emit("error", source="trade", message=f"buy {analysis.ticker} failed")

    elif analysis.signal == "SELL" and has_position:
        try:
            order = await io.trader.close_position(analysis.ticker)
            log.info("closed", ticker=analysis.ticker, order_id=order.id if order else None)
            if io.state is not None:
                io.state.emit("trade", action="sell", ticker=analysis.ticker)
        except Exception:
            log.exception("close_failed", ticker=analysis.ticker)
            if io.state is not None:
                io.state.emit("error", source="trade", message=f"close {analysis.ticker} failed")
