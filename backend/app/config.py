from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_db_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    target_db_url: str = "postgresql+asyncpg://readonly:readonly@localhost:5432/target"

    llm_provider: Literal["ollama", "openai"] = "ollama"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = "unused"
    llm_model: str = "qwen2.5-coder:7b"
    embed_model: str = "nomic-embed-text"
    llm_timeout_s: float = 60.0

    retrieve_top_k: int = 4
    max_rows: int = 500
    statement_timeout_ms: int = 5000
    prompt_version: str = "v1"
    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
