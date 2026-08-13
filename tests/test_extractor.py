from wsb_trader.extractor import (
    CASHTAG_RE,
    BARE_RE,
    STOP_TERMS,
    TickerMention,
    extract,
    aggregate,
)


class TestCashtagExtraction:
    def test_single_cashtag(self):
        result = extract("I'm long $AAPL to the moon")
        assert result == [TickerMention("AAPL", 1, 1)]

    def test_multiple_cashtags(self):
        result = extract("$TSLA calls printed but $GME still holding")
        tickers = {m.ticker for m in result}
        assert tickers == {"TSLA", "GME"}

    def test_cashtag_with_class_suffix(self):
        result = extract("Bought $BRK.B for the long term")
        assert result[0].ticker == "BRK.B"

    def test_cashtag_not_matched_inside_word(self):
        result = extract("email me at fin$AAPL is not a ticker")
        assert result == []

    def test_repeated_cashtag_counted(self):
        result = extract("$NVDA $NVDA $NVDA to the moon")
        assert result[0] == TickerMention("NVDA", 3, 3)


class TestBareTickerExtraction:
    def test_bare_ticker_in_prose(self):
        result = extract("AAPL earnings beat expectations")
        assert TickerMention("AAPL", 1, 0) in result

    def test_stop_words_filtered(self):
        # THE, AND, FOR, YOLO, etc. should not appear as tickers.
        text = "THE WSB YOLO on AAPL AND TSLA FOR the win"
        result = extract(text)
        tickers = {m.ticker for m in result}
        assert tickers == {"AAPL", "TSLA"}
        assert "THE" not in tickers
        assert "WSB" not in tickers
        assert "YOLO" not in tickers

    def test_lowercase_ignored(self):
        # ``aapl`` in lowercase should not be extracted.
        result = extract("aapl is a great company")
        assert result == []

    def test_valid_tickers_filter(self):
        text = "ZZZZ is a fake ticker but AAPL is real"
        # Without filter, both pass extraction.
        assert {m.ticker for m in extract(text)} == {"ZZZZ", "AAPL"}
        # With filter, only AAPL survives.
        filtered = extract(text, valid_tickers=frozenset({"AAPL"}))
        assert {m.ticker for m in filtered} == {"AAPL"}

    def test_cashtag_kept_even_if_not_in_valid_set(self):
        # A $ prefix is a strong-enough signal to override the filter —
        # someone typing ``$XYZ`` almost certainly means the ticker XYZ,
        # even if it's not (yet) in our valid set.
        result = extract("$XYZ", valid_tickers=frozenset({"AAPL"}))
        assert result == [TickerMention("XYZ", 1, 1)]


class TestMixedExtraction:
    def test_cashtag_and_bare_merged(self):
        # $AAPL cashtag + AAPL bare should combine into one entry.
        result = extract("$AAPL to the moon, AAPL is undervalued")
        assert result == [TickerMention("AAPL", 2, 1)]

    def test_sort_order_by_count_desc(self):
        text = "AAPL AAPL AAPL TSLA TSLA NVDA"
        result = extract(text)
        assert [m.ticker for m in result] == ["AAPL", "TSLA", "NVDA"]

    def test_empty_text(self):
        assert extract("") == []

    def test_no_tickers(self):
        assert extract("no tickers here just words") == []


class TestAggregate:
    def test_merge_two_documents(self):
        a = extract("$AAPL calls")
        b = extract("$AAPL puts, $TSLA calls")
        merged = aggregate([a, b])
        by_ticker = {m.ticker: m for m in merged}
        assert by_ticker["AAPL"].count == 2
        assert by_ticker["TSLA"].count == 1

    def test_aggregate_sort_order(self):
        docs = [
            extract("$TSLA"),
            extract("$AAPL $AAPL"),
            extract("$GME"),
        ]
        merged = aggregate(docs)
        # AAPL has 2 mentions (comes first); TSLA and GME tie at 1, so they
        # break ties alphabetically -> GME before TSLA.
        assert [m.ticker for m in merged] == ["AAPL", "GME", "TSLA"]

    def test_empty_aggregate(self):
        assert aggregate([]) == []


class TestRegexSanity:
    """Guard against regex regressions on tricky inputs."""

    def test_cashtag_at_start(self):
        assert CASHTAG_RE.findall("$AAPL rally") == ["AAPL"]

    def test_cashtag_at_end(self):
        assert CASHTAG_RE.findall("long on $AAPL") == ["AAPL"]

    def test_bare_at_boundaries(self):
        assert "AAPL" in BARE_RE.findall("AAPL is up")
        assert "AAPL" in BARE_RE.findall("up AAPL today")
        assert "AAPL" in BARE_RE.findall("(AAPL)")

    def test_bare_ignores_lowercase(self):
        assert BARE_RE.findall("aapl") == []

    def test_stop_terms_is_frozenset(self):
        assert isinstance(STOP_TERMS, frozenset)
        assert "THE" in STOP_TERMS
        assert "YOLO" in STOP_TERMS
