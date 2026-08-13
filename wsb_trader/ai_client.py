"""LLM client for sentiment/signal analysis of Reddit posts about a ticker.

Talks to any OpenAI chat-completions-compatible endpoint. Works out of the
box with OpenAI, Groq, OpenRouter, Together, and local runtimes like Ollama
that expose an OpenAI-compat surface at ``/v1/chat/completions``.

We deliberately use raw ``httpx`` instead of the ``openai`` SDK so the
integration is portable across provider SDK versions and easy to mock.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

import httpx


Signal = Literal["BUY", "HOLD", "SELL"]


@dataclass(frozen=True)
class TickerAnalysis:
    ticker: str
    signal: Signal
    confidence: float  # 0.0 - 1.0
    reasoning: str


SYSTEM_PROMPT = """You are a trading sentiment analyst reading r/wallstreetbets posts.

For a given ticker and a set of post excerpts mentioning it, output a JSON
object with three fields:
- "signal": one of "BUY", "HOLD", "SELL"
- "confidence": a float between 0.0 and 1.0 reflecting how certain you are
- "reasoning": one or two short sentences explaining the call

Treat WSB posts as noisy, meme-heavy, and often contrarian. Rocket emojis,
"YOLO", and "diamond hands" indicate bullish sentiment; "loss porn",
"puts", and "bagholder" indicate bearish sentiment. Ignore posts that are
just complaints or off-topic.

Output ONLY the JSON object, no prose before or after."""


class AIClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
    ):
        if not api_key:
            raise ValueError("AI_API_KEY must be set")
        if not base_url:
            raise ValueError("AI_BASE_URL must be set")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def analyze(self, ticker: str, excerpts: list[str]) -> TickerAnalysis:
        """Ask the LLM to produce a signal for one ticker given post excerpts."""
        if not excerpts:
            # Nothing to analyze; return a low-confidence HOLD rather than
            # calling the model with empty input.
            return TickerAnalysis(ticker, "HOLD", 0.0, "no excerpts provided")

        user_msg = _build_user_message(ticker, excerpts)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return _parse_response(ticker, content)


def _build_user_message(ticker: str, excerpts: list[str]) -> str:
    # Cap excerpts to keep the request size reasonable regardless of how many
    # posts mentioned the ticker.
    trimmed = [e[:500] for e in excerpts[:20]]
    joined = "\n---\n".join(trimmed)
    return f"Ticker: {ticker}\n\nPost excerpts:\n{joined}"


def _parse_response(ticker: str, content: str) -> TickerAnalysis:
    """Parse the JSON payload the model returned, tolerating light noise.

    Providers occasionally wrap JSON in code fences even when we ask for
    strict JSON output; strip a fence if present, then parse.
    """
    stripped = content.strip()
    # Strip ```json ... ``` fences if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1)
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise AIResponseError(f"model returned non-JSON: {content!r}") from e

    signal = obj.get("signal", "").upper()
    if signal not in ("BUY", "HOLD", "SELL"):
        raise AIResponseError(f"invalid signal: {signal!r}")

    confidence = obj.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        raise AIResponseError(f"invalid confidence: {confidence!r}")

    reasoning = str(obj.get("reasoning", ""))
    return TickerAnalysis(ticker, signal, float(confidence), reasoning)


class AIResponseError(Exception):
    """The AI provider returned a response we couldn't parse into a signal."""
