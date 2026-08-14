"""Extract stock and crypto tickers from free-form text (Reddit posts, comments, titles).

Two extraction paths:
  1. Cashtag form: ``$AAPL`` — high confidence, always kept.
  2. Bare uppercase form: ``AAPL`` in prose — filtered against a stop-list of
     WSB jargon and common English words that happen to look like tickers.

Crypto tickers (BTC, ETH, DOGE, etc.) are recognized and mapped to Alpaca's
symbol format (BTC -> BTCUSD) via ``to_alpaca_symbol``.

Callers can optionally supply a ``valid_tickers`` set (e.g. Alpaca's tradable
asset list) to further constrain matches. Without it, bare-form extraction is
best-effort and will produce false positives.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass


# Common crypto tickers Alpaca supports (paper trading). Extend as needed.
# These are always accepted as valid tickers, even bare-form (no cashtag).
CRYPTO_TICKERS: frozenset[str] = frozenset({
    "BTC", "ETH", "DOGE", "SOL", "AVAX", "MATIC", "LINK", "LTC", "BCH",
    "UNI", "AAVE", "SHIB", "DOT", "XRP", "ADA", "USDT", "USDC", "PEPE",
    "GRT", "MKR", "SUSHI", "YFI", "CRV", "BAT", "TRUMP",
})


# WSB slang, common English words, and abbreviations that pattern-match as
# tickers but aren't. Extend as needed.
STOP_TERMS: frozenset[str] = frozenset({
    # WSB jargon
    "WSB", "YOLO", "FD", "DD", "ATH", "ATL", "ITM", "OTM", "FOMO", "MOASS",
    "TA", "PT", "EOD", "EOW", "EOY", "IV", "OI", "PE", "PR", "IPO", "SPAC",
    "ETF", "CEO", "CFO", "COO", "CTO", "SEC", "FED", "FTC", "DOJ", "IRS",
    "GDP", "CPI", "PPI", "FOMC", "QE", "QT", "AH", "PM", "AM",
    "TLDR", "TLDR", "IMO", "IMHO", "AFAIK", "IIRC", "TIL", "OP",
    "LOL", "LMAO", "ROFL", "WTF", "OMG", "IDK", "IDGAF", "GTFO",
    # Common English words that are all-caps in headlines
    "A", "I", "AT", "BE", "BY", "DO", "GO", "HE", "IF", "IN", "IS", "IT",
    "ME", "MY", "NO", "OF", "ON", "OR", "SO", "TO", "UP", "US", "WE",
    "ALL", "AND", "ANY", "ARE", "BUT", "CAN", "FOR", "GET", "GOT", "HAS",
    "HAD", "HER", "HIM", "HIS", "HOW", "ITS", "MAY", "NEW", "NOT", "NOW",
    "OLD", "ONE", "OUR", "OUT", "OWN", "SEE", "SHE", "THE", "TOO", "TWO",
    "USE", "WAS", "WAY", "WHO", "WHY", "YES", "YOU",
    "FROM", "HAVE", "JUST", "LIKE", "MAKE", "MUCH", "MUST", "OVER",
    "SOME", "SUCH", "THAN", "THAT", "THEM", "THIS", "TRUE", "WHEN",
    "WITH", "YOUR", "GOOD", "GREAT", "WILL", "SHOULD", "COULD", "WOULD",
    # Financial abbreviations that aren't tradable tickers
    "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF", "CNY", "INR",
    "NYSE", "NASDAQ", "SPY",  # SPY is real but we drop broad-market ETFs to
                              # avoid the bot chasing index moves. Remove if unwanted.
    # Reddit-specific
    "AMA", "NSFW", "NSFL", "AITA",
    "USA", "UK", "EU",
    # Broker names / venues that appear a lot in posts
    "IBKR", "RH",  # Robinhood ticker is HOOD, RH is Restoration Hardware
})


# ``$TICKER`` cashtag: dollar sign, 1-5 uppercase letters, optional period+letters
# (BRK.B, BRK.A). Boundary on either side prevents matching inside words.
CASHTAG_RE = re.compile(r"(?<![\w$])\$([A-Z]{1,5}(?:\.[A-Z]{1,2})?)(?![\w.])")

# Bare uppercase token: 2-5 uppercase letters, word-boundary on both sides,
# not preceded by ``$`` (handled by CASHTAG_RE).
BARE_RE = re.compile(r"(?<![\w$])([A-Z]{2,5})(?![\w.])")


@dataclass(frozen=True)
class TickerMention:
    ticker: str
    count: int
    cashtag_count: int  # subset of count that came from $TICKER form


def extract(
    text: str,
    *,
    valid_tickers: frozenset[str] | None = None,
) -> list[TickerMention]:
    """Return tickers found in ``text``, sorted by total mentions descending.

    If ``valid_tickers`` is provided, bare-form matches are filtered to that
    set (cashtag matches are always kept — a ``$`` prefix is a strong signal).
    """
    cashtags = Counter(CASHTAG_RE.findall(text))
    bare = Counter(t for t in BARE_RE.findall(text) if t not in STOP_TERMS)

    if valid_tickers is not None:
        bare = Counter({t: c for t, c in bare.items() if t in valid_tickers})

    all_tickers = set(cashtags) | set(bare)
    mentions = [
        TickerMention(
            ticker=t,
            count=cashtags.get(t, 0) + bare.get(t, 0),
            cashtag_count=cashtags.get(t, 0),
        )
        for t in all_tickers
    ]
    mentions.sort(key=lambda m: (-m.count, m.ticker))
    return mentions


def aggregate(mention_lists: list[list[TickerMention]]) -> list[TickerMention]:
    """Merge extraction results from multiple documents (e.g. many posts)."""
    total: Counter[str] = Counter()
    cashtag_total: Counter[str] = Counter()
    for lst in mention_lists:
        for m in lst:
            total[m.ticker] += m.count
            cashtag_total[m.ticker] += m.cashtag_count
    return sorted(
        (TickerMention(t, total[t], cashtag_total[t]) for t in total),
        key=lambda m: (-m.count, m.ticker),
    )


def is_crypto(ticker: str) -> bool:
    """Return True if the ticker is a known crypto asset."""
    base = ticker.upper().removesuffix("USD").removesuffix("/USD")
    return base in CRYPTO_TICKERS


def to_alpaca_symbol(ticker: str) -> str:
    """Map a raw ticker to Alpaca's expected order symbol.

    Stocks pass through unchanged (``AAPL`` -> ``AAPL``). Crypto tickers get a
    ``USD`` suffix (``BTC`` -> ``BTCUSD``). Already-suffixed crypto tickers
    pass through (``BTCUSD`` -> ``BTCUSD``).
    """
    ticker = ticker.upper()
    if ticker.endswith("USD") and ticker[:-3] in CRYPTO_TICKERS:
        return ticker
    if ticker in CRYPTO_TICKERS:
        return f"{ticker}USD"
    return ticker
