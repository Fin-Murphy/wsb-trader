# wsb-trader

## Tremble before me, as I have built probably the worst performing trading bot of all time by attempting to mimic the habits of the denizens of r/WallStreetBets.

## This bot holds as many consecutive positions as his meager finances will allow, short sell, stop loss, and so much more. He is as senile as he is aggressive, and his trading decisions are based entirely off such oracles as Yahoo finance and... 4chan /biz (revolting). Download this bot at your own risk, and for the love of all that is good and holy, dont give it actual money to work with. 

Paper-trading bot that scrapes social/financial sources, uses an LLM to score sentiment on tickers, and places simulated trades via Alpaca's paper account. Ships with a live web dashboard.

**Paper only. Guarded by code — `PaperTrader.__init__` refuses to run against any base URL not containing "paper".**

## Pipeline

Every `POLL_INTERVAL_SECONDS`, the bot runs one **tick**. A tick is eight stages, wired in `wsb_trader/bot.py`:

```
┌───────────────────────────────────────────────────────────────────┐
│ 1. scrape       Parallel fetch from all enabled sources           │
│                 (4chan /biz/, StockTwits trending, Yahoo Finance) │
├───────────────────────────────────────────────────────────────────┤
│ 2. dedupe       Drop posts already seen in a previous tick        │
├───────────────────────────────────────────────────────────────────┤
│ 3. extract      Regex + stop-list → ticker mention counts         │
├───────────────────────────────────────────────────────────────────┤
│ 4. threshold    Keep tickers with count ≥ MIN_MENTIONS            │
├───────────────────────────────────────────────────────────────────┤
│ 5. route        For each candidate, gather posts mentioning it    │
├───────────────────────────────────────────────────────────────────┤
│ 6. analyze      One LLM call per ticker → BUY/HOLD/SELL + conf.   │
├───────────────────────────────────────────────────────────────────┤
│ 7. trade        Apply guardrails, place market orders via Alpaca  │
├───────────────────────────────────────────────────────────────────┤
│ 8. publish      Refresh account/positions and push to dashboard   │
└───────────────────────────────────────────────────────────────────┘
```

### 1. Scrape

Three scrapers, each returning objects that share a duck-typed `.combined_text` property. All fetches run concurrently; a failure in one source is logged and swallowed so the loop continues with whatever else was gathered.

| Source | File | What it fetches | Auth |
|--------|------|-----------------|------|
| 4chan `/biz/` | `scraper_4chan.py` | Catalog threads with ≥5 replies, HTML stripped from `sub`/`com` | None |
| StockTwits | `scraper_stocktwits.py` | Trending stream messages, StockTwits' own `[Bullish]`/`[Bearish]` tag appended to body | None |
| Yahoo Finance | `scraper_yahoo.py` | (a) Trending symbols as synthetic `$AAPL $AAPL $AAPL trending on Yahoo Finance` posts + (b) recent market news headlines + summaries | None |

**Yahoo trending trick**: repeating the cashtag 3× per synthetic post guarantees the extractor counts each trending symbol at least 3 times — enough to clear `MIN_MENTIONS=3` on its own even before other sources chime in.

### 2. Dedupe

Each post has an ID (`.id` for StockTwits, `.no` for 4chan) or none (Yahoo). The bot keeps `seen_ids: set[str]` namespaced by class (e.g. `BizThread:12345`). Posts with a known ID are dropped. Yahoo posts have no ID so they're always processed — their text is highly repetitive so this doesn't matter for scoring.

### 3. Extract tickers (`extractor.py`)

Two regexes scan each post's `combined_text`:

- **Cashtag** `$TICKER`: always kept — dollar sign is a strong signal.
- **Bare uppercase** `AAPL`: kept only if not in `STOP_TERMS`. That set filters:
  - WSB slang (`YOLO`, `FOMO`, `DD`, `MOASS`, …)
  - Common English words (`THE`, `AND`, `NOT`, …)
  - Financial abbreviations that pattern-match (`SEC`, `NYSE`, `CEO`, currency codes, …)
  - `SPY` — deliberately excluded so the bot doesn't chase broad-market moves. Remove from `STOP_TERMS` if you want to trade it.

Each post → `list[TickerMention(ticker, count, cashtag_count)]`. `aggregate()` merges across all posts for the tick and sorts descending by count.

### 4. Threshold filter

Only tickers with `count >= MIN_MENTIONS` (default 3) advance. This is the key noise filter — a single stray mention on one platform never triggers analysis.

### 5. Per-ticker excerpt routing

For each surviving ticker, gather only the posts that actually mentioned it. The LLM never sees the full corpus — it sees only excerpts about the ticker it's judging. Excerpts are then capped at 20 per ticker × 500 characters each in `AIClient._build_user_message`.

### 6. AI analysis (`ai_client.py`)

One LLM call per ticker, in parallel via `asyncio.gather`. Any OpenAI-compatible endpoint works (OpenAI, Groq, OpenRouter, Together, Ollama).

**System prompt** (fixed, in `ai_client.py`):

> You are a trading sentiment analyst reading r/wallstreetbets posts.
>
> For a given ticker and a set of post excerpts mentioning it, output a JSON object with three fields:
> - `"signal"`: one of `"BUY"`, `"HOLD"`, `"SELL"`
> - `"confidence"`: a float between 0.0 and 1.0 reflecting how certain you are
> - `"reasoning"`: one or two short sentences explaining the call
>
> Treat WSB posts as noisy, meme-heavy, and often contrarian. Rocket emojis, "YOLO", and "diamond hands" indicate bullish sentiment; "loss porn", "puts", and "bagholder" indicate bearish sentiment. Ignore posts that are just complaints or off-topic.
>
> Output ONLY the JSON object, no prose before or after.

**User prompt**: `Ticker: {TICKER}\n\nPost excerpts:\n{joined}` where joined is excerpts separated by `\n---\n`.

**Request settings**:
- `temperature=0.1` — low, for consistent classification (not creative writing)
- `max_tokens=150` — the JSON reply is ~50 tokens; 150 leaves headroom
- `response_format={"type": "json_object"}` — provider-side JSON enforcement

**Response validation** (`_parse_response`):
- Strips ```` ```json ```` fences if present (some providers wrap even when told not to)
- Signal must be one of the three allowed values
- Confidence must be numeric in [0.0, 1.0]
- Malformed responses raise `AIResponseError`; that ticker is skipped for the tick, others proceed

### 7. Trade execution (`_execute_signal` in `bot.py`)

Rules applied in order:

| Signal | Position held? | Confidence ≥ `MIN_CONFIDENCE`? | At `MAX_POSITIONS`? | Action |
|--------|---------------|--------------------------------|---------------------|--------|
| BUY  | No  | Yes | No  | Market buy `POSITION_SIZE_USD` notional |
| BUY  | No  | Yes | Yes | Skip — bounded portfolio |
| BUY  | Yes | —   | —   | Skip — never averages up |
| SELL | Yes | Yes | —   | Close entire position |
| SELL | No  | —   | —   | Skip — no shorting |
| HOLD | —   | —   | —   | Skip |
| Any  | —   | No  | —   | Skip — below confidence gate |

Alpaca rejects some tickers as non-tradable (OTC, delisted, etc.). These raise `AssetNotTradable` and are logged as a warning + shown in the dashboard event feed. Other errors log a traceback but don't stop the tick.

### 8. Dashboard publish

After all trades, the bot re-fetches account + positions from Alpaca and emits a `positions` event through the shared `DashboardState`. The dashboard updates in real time via SSE.

## Trading philosophy

Sentiment-momentum with hard guardrails:

- **AI is a classifier, not an oracle.** It emits a label + confidence; the bot's own rules turn those into trades.
- **Multi-source triangulation** (`MIN_MENTIONS`) filters lone voices — a ticker needs cross-source chatter to be analyzed at all.
- **Confidence gate** (`MIN_CONFIDENCE`, default 0.75) filters weak signals.
- **Uniform position size** (`POSITION_SIZE_USD`, default $1000) so wins and losses are comparable.
- **Bounded concurrency** (`MAX_POSITIONS`, default 5) so risk can't stack.
- **No averaging up** — BUY on an existing position is a no-op.
- **No shorting** — SELL only closes existing longs.
- **Paper-only enforcement** — `PaperTrader.__init__` refuses non-paper URLs.

## Dashboard

Live web UI on `http://<host>:8080` (override with `DASHBOARD_PORT`). FastAPI + Server-Sent Events, single embedded HTML page, no build step.

- **Account stats** — cash, buying power, portfolio value, equity
- **Positions table** — symbol, qty, market value, unrealized P&L, avg entry
- **Last tick's ticker mentions** — bar heatmap, top 25 by count (green = includes at least one cashtag mention)
- **Live event feed** — scrolling log of `scrape`, `mentions`, `signal`, `trade`, `positions`, and `error` events

The bot writes to `DashboardState` (`wsb_trader/state.py`), an in-memory pub/sub store. The FastAPI app (`wsb_trader/dashboard.py`) exposes three endpoints:

- `GET /` — single-page HTML
- `GET /api/snapshot` — current state as JSON (used on page load)
- `GET /stream` — SSE stream with 20s heartbeats (keeps proxied/tunneled connections alive)

Uvicorn runs in the same asyncio event loop as APScheduler — one process, no IPC.

## Config

All configuration via environment variables in `.env` at the project root. The path is resolved relative to the source tree so the bot works from any working directory.

### Alpaca

- `ALPACA_API_KEY` / `ALPACA_API_SECRET` — from the [Alpaca paper trading dashboard](https://alpaca.markets)
- `ALPACA_BASE_URL` — defaults to `https://paper-api.alpaca.markets`. Must contain `"paper"` or the bot refuses to start.

### AI provider

- `AI_BASE_URL` — any OpenAI-compatible endpoint (`https://api.openai.com/v1`, `https://api.groq.com/openai/v1`, `http://localhost:11434/v1` for Ollama, …)
- `AI_API_KEY` — provider key
- `AI_MODEL` — model name (`gpt-4o-mini`, `llama-3.1-70b-versatile`, …)
- `AI_TEMPERATURE` — default `0.1`
- `AI_MAX_TOKENS` — default `150`

### Data source toggles

- `ENABLE_YAHOO` — default `true`
- `ENABLE_STOCKTWITS` — default `true`
- `ENABLE_4CHAN` — default `false` (opt-in, noisy)

### Trading parameters

- `POLL_INTERVAL_SECONDS` — tick cadence, default `300`
- `MIN_MENTIONS` — cross-source mention threshold, default `3`
- `MIN_CONFIDENCE` — AI confidence gate, default `0.75`
- `POSITION_SIZE_USD` — notional per buy, default `1000`
- `MAX_POSITIONS` — concurrent position cap, default `5`

### Dashboard

- `DASHBOARD_PORT` — default `8080`

## Running

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env   # then fill in ALPACA_API_KEY/SECRET, AI_API_KEY
python -m wsb_trader
```

Then open `http://<host>:8080` in a browser. If accessing over SSH, forward the port:

```bash
ssh -L 8080:localhost:8080 user@host
```

## Tests

```bash
pytest -v
```

Tests never hit the network — HTTP calls are mocked with `respx`, and the tick loop is exercised end-to-end with mocked scrapers/AI/trader in `tests/test_bot.py`.

## Module map

| Module | Responsibility |
|--------|---------------|
| `bot.py` | The `tick()` coordinator — orchestrates the 8-stage pipeline. |
| `state.py` | In-memory `DashboardState` with pub/sub event bus for the dashboard. |
| `dashboard.py` | FastAPI app: HTML page, snapshot JSON, SSE stream. |
| `extractor.py` | Ticker extraction (cashtag + bare) with `STOP_TERMS` filter. |
| `ai_client.py` | LLM client, prompt construction, response validation. |
| `trader.py` | Alpaca REST client. Enforces paper-only. |
| `scraper_4chan.py`, `scraper_stocktwits.py`, `scraper_yahoo.py` | Source scrapers. |
| `config.py` | Env-var loading with strict validation. |
| `__main__.py` | Entry point — wires dependencies, starts scheduler + uvicorn. |
