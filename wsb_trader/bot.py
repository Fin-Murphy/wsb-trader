"""One iteration of the trading loop, wired end-to-end."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from wsb_trader.ai_client import AIClient, AIResponseError, TickerAnalysis
from wsb_trader.config import Config
from wsb_trader.extractor import aggregate, extract, is_crypto, to_alpaca_symbol
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

    log.info("scraped", n_posts=len(posts), seen=n_dupes)
    if io.state is not None:
        io.state.last_tick_at = datetime.now(timezone.utc).isoformat()
        io.state.emit(
            "scrape",
            n_posts=len(posts),
            n_dupes=n_dupes,
            sources=[type(s).__name__ for s in io.scrapers],
        )

    if not posts:
        log.info("no_new_posts")
        return

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
        account = await io.trader.get_account()
    except Exception:
        log.exception("list_positions_failed")
        return

    await _check_position_exits(io, current_positions)
    await _fetch_and_emit_prices(io, current_positions)

    for analysis in analyses:
        if analysis is None:
            continue
        await _execute_signal(io, analysis, current_positions, account)

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


def _position_notional(config: Config, account: Any) -> Decimal:
    """Compute the $ notional for a new position.

    If ``position_size_pct`` > 0, size as that % of current buying power so
    utilization scales with the account. Otherwise fall back to the fixed
    ``position_size_usd``. Rounded to whole dollars.
    """
    if config.position_size_pct > 0 and account is not None:
        buying_power = Decimal(str(account.buying_power))
        pct = Decimal(str(config.position_size_pct)) / Decimal("100")
        sized = (buying_power * pct).quantize(Decimal("1"))
        if sized > 0:
            return sized
    return config.position_size_usd


async def _fetch_and_emit_prices(
    io: TickIO,
    current_positions: dict[str, object],
) -> None:
    """Fetch and emit current prices for all positions."""
    if io.state is None:
        return

    prices = await asyncio.gather(*(
        io.trader.get_current_price(ticker)
        for ticker in current_positions.keys()
    ))

    position_prices = []
    for (ticker, position), price in zip(current_positions.items(), prices):
        if price is not None and hasattr(position, "avg_entry_price"):
            change = price - position.avg_entry_price
            change_pct = (change / position.avg_entry_price * 100) if position.avg_entry_price > 0 else 0
            position_prices.append({
                "ticker": ticker,
                "current_price": str(price),
                "entry_price": str(position.avg_entry_price),
                "change": str(change),
                "change_pct": round(float(change_pct), 2),
            })

    if position_prices:
        io.state.emit("prices", positions=position_prices)


async def _check_position_exits(
    io: TickIO,
    current_positions: dict[str, object],
) -> None:
    """Close positions that hit profit-taking or stop-loss thresholds."""
    for ticker, position in current_positions.items():
        if not hasattr(position, "unrealized_pl") or not hasattr(position, "market_value"):
            continue

        unrealized_pl = position.unrealized_pl
        market_value = position.market_value

        if market_value <= 0:
            continue

        pct_change = (unrealized_pl / market_value) * 100

        should_exit = False
        reason = ""

        if pct_change >= io.config.profit_target_pct:
            should_exit = True
            reason = f"profit_target_hit ({pct_change:.1f}%)"
        elif pct_change <= io.config.stop_loss_pct:
            should_exit = True
            reason = f"stop_loss_triggered ({pct_change:.1f}%)"

        if should_exit:
            try:
                order = await io.trader.close_position(ticker)
                log.info("position_exited", ticker=ticker, reason=reason, order_id=order.id if order else None)
                if io.state is not None:
                    io.state.emit("trade", action="exit", ticker=ticker, reason=reason)
            except Exception:
                log.exception("exit_failed", ticker=ticker, reason=reason)
                if io.state is not None:
                    io.state.emit("error", source="trade", message=f"exit {ticker} failed: {reason}")


async def _execute_signal(
    io: TickIO,
    analysis: TickerAnalysis,
    current_positions: dict[str, object],
    account: Any,
) -> None:
    trade_symbol = to_alpaca_symbol(analysis.ticker)
    has_position = trade_symbol in current_positions
    notional = _position_notional(io.config, account)
    log.info(
        "signal",
        ticker=analysis.ticker,
        symbol=trade_symbol,
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
            log.info("at_max_positions_skipping_buy", ticker=trade_symbol)
            return
        try:
            order = await io.trader.place_market_order(
                trade_symbol,
                notional=notional,
                side="buy",
            )
            log.info("bought", ticker=trade_symbol, notional=str(notional), order_id=order.id)
            if io.state is not None:
                io.state.emit(
                    "trade",
                    action="buy",
                    ticker=trade_symbol,
                    notional=str(notional),
                    order_id=order.id,
                )
        except AssetNotTradable:
            log.warning("asset_not_tradable", ticker=trade_symbol)
            if io.state is not None:
                io.state.emit("error", source="trade", message=f"{trade_symbol} not tradable on Alpaca")
        except Exception:
            log.exception("buy_failed", ticker=trade_symbol)
            if io.state is not None:
                io.state.emit("error", source="trade", message=f"buy {trade_symbol} failed")

    elif analysis.signal == "SELL":
        if analysis.confidence < io.config.min_sell_confidence:
            return
        if has_position:
            try:
                order = await io.trader.close_position(trade_symbol)
                log.info("closed", ticker=trade_symbol, order_id=order.id if order else None)
                if io.state is not None:
                    io.state.emit("trade", action="sell", ticker=trade_symbol)
            except Exception:
                log.exception("close_failed", ticker=trade_symbol)
                if io.state is not None:
                    io.state.emit("error", source="trade", message=f"close {trade_symbol} failed")
        elif (
            io.config.enable_shorting
            and not is_crypto(trade_symbol)
            and len(current_positions) < io.config.max_positions
        ):
            try:
                order = await io.trader.place_short_order(
                    trade_symbol,
                    notional=notional,
                )
                log.info("shorted", ticker=trade_symbol, notional=str(notional), order_id=order.id)
                if io.state is not None:
                    io.state.emit(
                        "trade",
                        action="short",
                        ticker=trade_symbol,
                        notional=str(notional),
                        order_id=order.id,
                    )
            except AssetNotTradable:
                log.warning("asset_not_tradable_for_short", ticker=trade_symbol)
            except Exception:
                log.exception("short_failed", ticker=trade_symbol)
                if io.state is not None:
                    io.state.emit("error", source="trade", message=f"short {trade_symbol} failed")
