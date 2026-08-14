# wsb-trader

Paper-trading bot that scrapes multiple social/financial sources, uses an LLM to score sentiment on tickers mentioned, and places simulated trades via Alpaca's paper account.

## Architecture

```
[Reddit WSB]  [Yahoo Finance]  [StockTwits]  [4chan /biz/]
      └──────────────┬──────────────┘───────────┘
                     ↓ (parallel fetch)
              [extractor] → cashtags and $TICKER symbols
                     ↓
              [ai_client] → per-ticker sentiment + confidence (BUY/HOLD/SELL, 0.0–1.0)
                     ↓
              [trader] → paper orders on Alpaca
                     ↓
              [main loop, APScheduler-driven]
```

## Config

All configuration is via environment variables (see `.env.example`).

### Alpaca

- `ALPACA_API_KEY` / `ALPACA_API_SECRET` — from https://alpaca.markets paper trading dashboard
- `ALPACA_BASE_URL` — defaults to `https://paper-api.alpaca.markets` (paper only)

### AI provider

- `AI_BASE_URL` — any OpenAI-compatible endpoint (e.g. `https://api.openai.com/v1`, `https://api.groq.com/openai/v1`, or `http://localhost:11434/v1` for Ollama)
- `AI_API_KEY` — key for the AI provider
- `AI_MODEL` — model name (e.g. `gpt-4o-mini`, `gpt-4.1-nano`, `llama-3.1-70b-versatile`)
- `AI_TEMPERATURE` — sampling temperature; default `0.1` (low for consistent classification)
- `AI_MAX_TOKENS` — max tokens per response; default `150` (the JSON reply is ~50 tokens)

### Reddit (required if `ENABLE_REDDIT=true`)

Register a **script**-type app at https://www.reddit.com/prefs/apps. Set the redirect URI to `http://localhost:8080` (unused but required by Reddit).

- `REDDIT_USER_AGENT` — e.g. `wsb-trader/0.1 by u/yourhandle`
- `REDDIT_CLIENT_ID` — from the app registration page
- `REDDIT_CLIENT_SECRET` — from the app registration page

### Data source toggles

- `ENABLE_REDDIT` — default `true`
- `ENABLE_YAHOO` — default `true` (trending tickers + news headlines, no auth required)
- `ENABLE_STOCKTWITS` — default `true` (trending stream, no auth required)
- `ENABLE_4CHAN` — default `false` (/biz/ catalog, opt-in due to noise)

### Trading parameters

- `POLL_INTERVAL_SECONDS` — how often to run the loop; default `300`
- `MIN_MENTIONS` — minimum cross-source mention count before a ticker is analyzed; default `3`
- `MIN_CONFIDENCE` — minimum AI confidence to act on a signal; default `0.75`
- `POSITION_SIZE_USD` — notional size of each paper buy; default `1000`
- `MAX_POSITIONS` — max open positions at once; default `5`

## Running

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — fill in ALPACA_API_KEY/SECRET, AI_API_KEY, REDDIT_* if using Reddit
python -m wsb_trader
```

## Tests

```bash
pytest -v
```

Paper only. No real money.
