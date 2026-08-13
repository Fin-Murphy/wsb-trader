import httpx
import pytest
import respx

from wsb_trader.ai_client import (
    AIClient,
    AIResponseError,
    TickerAnalysis,
    _build_user_message,
    _parse_response,
)


def _chat_response(content: str) -> dict:
    """OpenAI-style chat completion response envelope."""
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
    }


class TestBuildUserMessage:
    def test_includes_ticker_and_excerpts(self):
        msg = _build_user_message("AAPL", ["post 1", "post 2"])
        assert "Ticker: AAPL" in msg
        assert "post 1" in msg
        assert "post 2" in msg

    def test_caps_at_20_excerpts(self):
        excerpts = [f"post {i}" for i in range(50)]
        msg = _build_user_message("AAPL", excerpts)
        assert "post 19" in msg
        assert "post 20" not in msg

    def test_truncates_long_excerpts(self):
        # Use ``@`` so we don't collide with literal chars in the prompt template.
        long_excerpt = "@" * 1000
        msg = _build_user_message("AAPL", [long_excerpt])
        # Each excerpt is capped at 500 chars.
        assert msg.count("@") == 500


class TestParseResponse:
    def test_parses_valid_json(self):
        content = '{"signal": "BUY", "confidence": 0.85, "reasoning": "bullish"}'
        result = _parse_response("AAPL", content)
        assert result == TickerAnalysis("AAPL", "BUY", 0.85, "bullish")

    def test_uppercases_signal(self):
        # Model might return lowercase; we normalize.
        content = '{"signal": "buy", "confidence": 0.9, "reasoning": ""}'
        result = _parse_response("AAPL", content)
        assert result.signal == "BUY"

    def test_strips_json_code_fence(self):
        content = '```json\n{"signal": "SELL", "confidence": 0.7, "reasoning": "bearish"}\n```'
        result = _parse_response("AAPL", content)
        assert result.signal == "SELL"

    def test_strips_bare_code_fence(self):
        content = '```\n{"signal": "HOLD", "confidence": 0.5, "reasoning": "mixed"}\n```'
        result = _parse_response("AAPL", content)
        assert result.signal == "HOLD"

    def test_rejects_non_json(self):
        with pytest.raises(AIResponseError, match="non-JSON"):
            _parse_response("AAPL", "sorry I can't help with that")

    def test_rejects_invalid_signal(self):
        content = '{"signal": "MAYBE", "confidence": 0.5, "reasoning": ""}'
        with pytest.raises(AIResponseError, match="invalid signal"):
            _parse_response("AAPL", content)

    def test_rejects_out_of_range_confidence(self):
        content = '{"signal": "BUY", "confidence": 1.5, "reasoning": ""}'
        with pytest.raises(AIResponseError, match="invalid confidence"):
            _parse_response("AAPL", content)

    def test_rejects_non_numeric_confidence(self):
        content = '{"signal": "BUY", "confidence": "high", "reasoning": ""}'
        with pytest.raises(AIResponseError, match="invalid confidence"):
            _parse_response("AAPL", content)


class TestAIClientInit:
    def test_rejects_empty_api_key(self):
        with pytest.raises(ValueError, match="AI_API_KEY"):
            AIClient(base_url="https://api.openai.com/v1", api_key="", model="x")

    def test_rejects_empty_base_url(self):
        with pytest.raises(ValueError, match="AI_BASE_URL"):
            AIClient(base_url="", api_key="k", model="x")

    def test_strips_trailing_slash_from_base_url(self):
        c = AIClient(base_url="https://api.openai.com/v1/", api_key="k", model="x")
        assert c.base_url == "https://api.openai.com/v1"


class TestAIClientAnalyze:
    @pytest.fixture
    def client(self):
        return AIClient(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            model="gpt-4o-mini",
        )

    async def test_empty_excerpts_returns_hold_without_calling_api(self, client):
        # No HTTP mock — if the code tried to call the API, respx would 404.
        result = await client.analyze("AAPL", [])
        await client.close()
        assert result.signal == "HOLD"
        assert result.confidence == 0.0

    @respx.mock
    async def test_analyze_sends_correct_request(self, client):
        route = respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_chat_response(
                '{"signal": "BUY", "confidence": 0.8, "reasoning": "rockets"}'
            ))
        )
        await client.analyze("GME", ["to the moon"])
        await client.close()
        assert route.called
        req = route.calls.last.request
        assert req.headers["authorization"] == "Bearer test-key"
        import json as _json
        body = _json.loads(req.content)
        assert body["model"] == "gpt-4o-mini"
        assert body["response_format"] == {"type": "json_object"}
        assert any("GME" in m["content"] for m in body["messages"])
        assert any("to the moon" in m["content"] for m in body["messages"])

    @respx.mock
    async def test_analyze_parses_response(self, client):
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_chat_response(
                '{"signal": "SELL", "confidence": 0.65, "reasoning": "loss porn"}'
            ))
        )
        result = await client.analyze("GME", ["loss porn everywhere"])
        await client.close()
        assert result == TickerAnalysis("GME", "SELL", 0.65, "loss porn")

    @respx.mock
    async def test_analyze_propagates_http_error(self, client):
        respx.post("https://api.openai.com/v1/chat/completions").mock(
            return_value=httpx.Response(500, json={"error": "server"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.analyze("AAPL", ["some post"])
        await client.close()

    @respx.mock
    async def test_works_with_alternate_base_url(self):
        """Sanity check: any OpenAI-compat endpoint should work — Groq, Ollama, etc."""
        client = AIClient(
            base_url="http://localhost:11434/v1",
            api_key="ollama",
            model="llama3.1",
        )
        route = respx.post("http://localhost:11434/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_chat_response(
                '{"signal": "HOLD", "confidence": 0.5, "reasoning": "mixed"}'
            ))
        )
        result = await client.analyze("AAPL", ["some post"])
        await client.close()
        assert route.called
        assert result.signal == "HOLD"
