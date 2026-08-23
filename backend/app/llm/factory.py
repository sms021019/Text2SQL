from app.config import Settings
from app.llm.base import LLMClient
from app.llm.ollama import OllamaClient
from app.llm.openai_compat import OpenAICompatClient


def build_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "ollama":
        return OllamaClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            embed_model=settings.embed_model,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout_s,
        )
    return OpenAICompatClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        embed_model=settings.embed_model,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout_s,
    )
