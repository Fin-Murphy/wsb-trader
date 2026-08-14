"""Entry point: ``python -m wsb_trader``.

Wires up dependencies, starts an APScheduler-driven loop, and blocks forever.
Handles SIGINT/SIGTERM for graceful shutdown so no in-flight HTTP requests get
cut off mid-flight.
"""
from __future__ import annotations

import asyncio
import logging
import signal

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from wsb_trader.ai_client import AIClient
from wsb_trader.bot import TickIO, tick
from wsb_trader.config import load_config
from wsb_trader.scraper import RedditScraper
from wsb_trader.scraper_4chan import FourchanScraper
from wsb_trader.scraper_stocktwits import StockTwitsScraper
from wsb_trader.scraper_yahoo import YahooFinanceScraper
from wsb_trader.trader import PaperTrader


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )


async def main() -> None:
    _configure_logging()
    log = structlog.get_logger()
    cfg = load_config()

    scrapers = []
    closeable = []

    if cfg.enable_reddit:
        s = RedditScraper(
            user_agent=cfg.reddit_user_agent,
            client_id=cfg.reddit_client_id,
            client_secret=cfg.reddit_client_secret,
        )
        scrapers.append(s)
        closeable.append(s)

    if cfg.enable_4chan:
        s = FourchanScraper()
        scrapers.append(s)
        closeable.append(s)

    if cfg.enable_yahoo:
        s = YahooFinanceScraper()
        scrapers.append(s)
        closeable.append(s)

    if cfg.enable_stocktwits:
        s = StockTwitsScraper()
        scrapers.append(s)
        closeable.append(s)

    if not scrapers:
        raise RuntimeError("all data sources are disabled — nothing to scrape")

    ai = AIClient(base_url=cfg.ai_base_url, api_key=cfg.ai_api_key, model=cfg.ai_model)
    trader = PaperTrader(
        api_key=cfg.alpaca_api_key,
        api_secret=cfg.alpaca_api_secret,
        base_url=cfg.alpaca_base_url,
    )
    io = TickIO(scrapers=scrapers, ai=ai, trader=trader, config=cfg)

    # Sanity-check credentials before starting the loop.
    acct = await trader.get_account()
    log.info(
        "startup",
        cash=str(acct.cash),
        buying_power=str(acct.buying_power),
        portfolio_value=str(acct.portfolio_value),
        model=cfg.ai_model,
        poll_interval=cfg.poll_interval_seconds,
        sources=[type(s).__name__ for s in scrapers],
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        tick, "interval", seconds=cfg.poll_interval_seconds,
        args=[io], next_run_time=None,  # first tick fires now via kickoff below
    )
    scheduler.start()

    # Fire once immediately rather than waiting a full interval.
    asyncio.create_task(tick(io))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    log.info("running")
    await stop_event.wait()
    log.info("shutting_down")

    scheduler.shutdown(wait=False)
    for s in closeable:
        await s.close()
    await ai.close()
    await trader.close()


if __name__ == "__main__":
    asyncio.run(main())
