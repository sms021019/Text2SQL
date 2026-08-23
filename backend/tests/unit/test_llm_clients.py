import json

import httpx
import pytest

from app.config import Settings
from app.core.errors import LLMError
from app.llm.factory import build_llm
from app.llm.ollama import OllamaClient
from app.llm.openai_compat import OpenAICompatClient
from tests.fakes.llm import FakeLLM

# --- OllamaClient -----------------------------------------------------------


async def test_ollama_complete_parses_response_and_reports_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert body["model"] == "qwen2.5-coder:7b"
        assert body["stream"] is False
        assert body["options"]["temperature"] == 0.2
        assert body["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "user"},
        ]
        return httpx.Response(
            200,
            json={"message": {"content": "SELECT 1"}, "prompt_eval_count": 10, "eval_count": 3},
        )

    client = OllamaClient(
        base_url="http://localhost:11434",
        model="qwen2.5-coder:7b",
        embed_model="nomic-embed-text",
        api_key="unused",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    completion = await client.complete("sys", "user", temperature=0.2)
    assert completion.text == "SELECT 1"
    assert completion.usage.prompt_tokens == 10
    assert completion.usage.completion_tokens == 3
    assert completion.usage.latency_ms >= 0.0
    assert completion.model == "qwen2.5-coder:7b"


async def test_ollama_embed_posts_batch_input():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed"
        body = json.loads(request.content)
        assert body["input"] == ["a", "b"]
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    client = OllamaClient(
        base_url="http://localhost:11434/",
        model="m",
        embed_model="nomic-embed-text",
        api_key="unused",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    vectors = await client.embed(["a", "b"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


async def test_ollama_non_2xx_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = OllamaClient(
        base_url="http://localhost:11434",
        model="m",
        embed_model="e",
        api_key="unused",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        await client.complete("sys", "user")


async def test_ollama_malformed_2xx_chat_body_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = OllamaClient(
        base_url="http://localhost:11434",
        model="m",
        embed_model="e",
        api_key="unused",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        await client.complete("sys", "user")


async def test_ollama_non_json_2xx_chat_body_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = OllamaClient(
        base_url="http://localhost:11434",
        model="m",
        embed_model="e",
        api_key="unused",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        await client.complete("sys", "user")


async def test_ollama_malformed_2xx_embed_body_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = OllamaClient(
        base_url="http://localhost:11434",
        model="m",
        embed_model="e",
        api_key="unused",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        await client.embed(["x"])


async def test_ollama_non_json_2xx_embed_body_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = OllamaClient(
        base_url="http://localhost:11434",
        model="m",
        embed_model="e",
        api_key="unused",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        await client.embed(["x"])


async def test_ollama_aclose_closes_underlying_httpx_client():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "x"}})

    client = OllamaClient(
        base_url="http://localhost:11434",
        model="m",
        embed_model="e",
        api_key="unused",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    await client.aclose()
    assert client._client.is_closed


# --- OpenAICompatClient -------------------------------------------------------


async def test_openai_complete_sends_bearer_auth_and_parses_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer sk-test"
        body = json.loads(request.content)
        assert body["model"] == "gpt-4o-mini"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "SELECT 1"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                "model": "gpt-4o-mini",
            },
        )

    client = OpenAICompatClient(
        base_url="https://api.openai.com",
        model="gpt-4o-mini",
        embed_model="text-embedding-3-small",
        api_key="sk-test",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    completion = await client.complete("sys", "user")
    assert completion.text == "SELECT 1"
    assert completion.usage.prompt_tokens == 12
    assert completion.usage.completion_tokens == 4
    assert completion.model == "gpt-4o-mini"


async def test_openai_embed_posts_to_embeddings_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2], "index": 0}]})

    client = OpenAICompatClient(
        base_url="https://api.openai.com",
        model="m",
        embed_model="text-embedding-3-small",
        api_key="sk-test",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    vectors = await client.embed(["hello"])
    assert vectors == [[0.1, 0.2]]


async def test_openai_embed_returns_vectors_in_request_order_regardless_of_response_order():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [0.9], "index": 1},
                    {"embedding": [0.1], "index": 0},
                ]
            },
        )

    client = OpenAICompatClient(
        base_url="https://api.openai.com",
        model="m",
        embed_model="e",
        api_key="k",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    vectors = await client.embed(["first", "second"])
    assert vectors == [[0.1], [0.9]]


async def test_openai_base_url_does_not_double_v1_suffix():
    seen_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, json={"data": [{"embedding": [0.1], "index": 0}]})

    client = OpenAICompatClient(
        base_url="https://api.openai.com/v1/",
        model="m",
        embed_model="e",
        api_key="k",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    await client.embed(["x"])
    assert seen_paths == ["/v1/embeddings"]


async def test_openai_non_2xx_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    client = OpenAICompatClient(
        base_url="https://api.openai.com",
        model="m",
        embed_model="e",
        api_key="bad",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        await client.embed(["x"])


async def test_openai_malformed_2xx_chat_body_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = OpenAICompatClient(
        base_url="https://api.openai.com",
        model="m",
        embed_model="e",
        api_key="k",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        await client.complete("sys", "user")


async def test_openai_non_json_2xx_chat_body_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = OpenAICompatClient(
        base_url="https://api.openai.com",
        model="m",
        embed_model="e",
        api_key="k",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        await client.complete("sys", "user")


async def test_openai_malformed_2xx_embed_body_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = OpenAICompatClient(
        base_url="https://api.openai.com",
        model="m",
        embed_model="e",
        api_key="k",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        await client.embed(["x"])


async def test_openai_non_json_2xx_embed_body_raises_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = OpenAICompatClient(
        base_url="https://api.openai.com",
        model="m",
        embed_model="e",
        api_key="k",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        await client.embed(["x"])


async def test_openai_aclose_closes_underlying_httpx_client():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.1], "index": 0}]})

    client = OpenAICompatClient(
        base_url="https://api.openai.com",
        model="m",
        embed_model="e",
        api_key="k",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )
    await client.aclose()
    assert client._client.is_closed


# --- build_llm ----------------------------------------------------------------


def test_build_llm_returns_ollama_client_for_ollama_provider():
    settings = Settings(_env_file=None, llm_provider="ollama")
    client = build_llm(settings)
    assert isinstance(client, OllamaClient)


def test_build_llm_returns_openai_client_for_openai_provider():
    settings = Settings(_env_file=None, llm_provider="openai")
    client = build_llm(settings)
    assert isinstance(client, OpenAICompatClient)


# --- FakeLLM --------------------------------------------------------------------


async def test_fake_llm_returns_list_responses_in_order():
    fake = FakeLLM(responses=["first", "second"])
    c1 = await fake.complete("sys", "u1")
    c2 = await fake.complete("sys", "u2")
    assert c1.text == "first"
    assert c2.text == "second"
    assert fake.calls == [("sys", "u1"), ("sys", "u2")]


async def test_fake_llm_list_exhausted_raises_assertion_error():
    fake = FakeLLM(responses=["only"])
    await fake.complete("sys", "u1")
    with pytest.raises(AssertionError, match="FakeLLM exhausted"):
        await fake.complete("sys", "u2")


async def test_fake_llm_dict_matches_by_substring():
    fake = FakeLLM(
        responses={"orders": "SELECT * FROM orders", "customers": "SELECT * FROM customers"}
    )
    completion = await fake.complete("sys", "how many orders were placed?")
    assert completion.text == "SELECT * FROM orders"


async def test_fake_llm_usage_tracks_token_estimates():
    fake = FakeLLM(responses=["1234567890AB"])  # 12 chars -> completion_tokens // 4 == 3
    completion = await fake.complete("sys", "abcdefgh")  # 8 chars -> prompt_tokens // 4 == 2
    assert completion.usage.prompt_tokens == 2
    assert completion.usage.completion_tokens == 3
    assert completion.usage.latency_ms == 0.0


async def test_fake_llm_embed_is_deterministic_and_normalized():
    fake = FakeLLM(dim=8)
    v1 = (await fake.embed(["hello world"]))[0]
    v2 = (await fake.embed(["hello world"]))[0]
    assert v1 == v2
    assert len(v1) == 8
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-6


async def test_fake_llm_embed_differs_for_different_text():
    fake = FakeLLM(dim=8)
    v1 = (await fake.embed(["alpha"]))[0]
    v2 = (await fake.embed(["beta"]))[0]
    assert v1 != v2
