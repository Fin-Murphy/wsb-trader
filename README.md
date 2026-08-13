# wsb-trader

Paper-trading bot that scrapes r/wallstreetbets, uses an LLM to score sentiment on tickers mentioned, and places simulated trades via Alpaca's paper account.

## Architecture

```
[r/wallstreetbets JSON API]
        ↓
[scraper] → recent hot/rising posts
        ↓
[extractor] → cashtags and $TICKER symbols
        ↓
[ai_client] → per-ticker sentiment + confidence (BUY/HOLD/SELL, 0.0-1.0)
        ↓
[trader] → paper orders on Alpaca (buy on high-confidence BUY, close on SELL)
        ↓
[main loop, APScheduler-driven]
```

## Config

All configuration is via environment variables (see `.env.example`).

- `ALPACA_API_KEY` / `ALPACA_API_SECRET` — from https://alpaca.markets paper trading dashboard
- `AI_BASE_URL` — OpenAI-compatible endpoint (e.g. `https://api.openai.com/v1`, `https://api.anthropic.com/v1`, `https://api.groq.com/openai/v1`, or local `http://localhost:11434/v1` for Ollama)
- `AI_API_KEY` — key for the AI provider
- `AI_MODEL` — model name (e.g. `gpt-4o-mini`, `claude-sonnet-4-6`, `llama-3.1-70b-versatile`)
- `REDDIT_USER_AGENT` — required by Reddit; format `wsb-trader/0.1 by u/<yourhandle>`

## Running

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env
python -m wsb_trader
```

## Tests

```bash
pytest -v
```

Paper only. No real money.
