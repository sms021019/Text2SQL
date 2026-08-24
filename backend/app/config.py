from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    app_db_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    target_db_url: str = "postgresql+asyncpg://readonly:readonly@localhost:5432/target"

    llm_provider: Literal["ollama", "openai"] = "ollama"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = "unused"
    llm_model: str = "qwen2.5-coder:7b"
    embed_model: str = "nomic-embed-text"
    llm_timeout_s: float = 60.0
    #: Cap on how long `create_app()`'s lifespan will wait for the initial
    #: `SchemaRetriever.build_index()` embedding call before giving up and
    #: continuing startup anyway (see `app.main`'s lifespan) -- deliberately
    #: shorter than `llm_timeout_s` so a slow-but-reachable LLM can't stall
    #: startup past the Docker `HEALTHCHECK`'s `--start-period`.
    startup_embed_timeout_s: float = 20.0

    retrieve_top_k: int = 4
    max_rows: int = 500
    statement_timeout_ms: int = 5000
    prompt_version: str = "v1"
    cors_origins: list[str] = ["http://localhost:5173"]

    redis_url: str = "redis://localhost:6379/0"
    cache_enabled: bool = True
    sql_cache_ttl_s: int = 86400
    result_cache_ttl_s: int = 300
    schema_cache_ttl_s: int = 604800

    log_level: str = "INFO"
    #: Path to the few-shot examples YAML, relative to the CWD the app is
    #: launched from (`backend/` locally; in the Docker image `seed/` is
    #: copied to `/app/seed`, so this is overridden there). Missing file is
    #: tolerated -- see `app.main._load_examples`.
    examples_path: str = "../seed/questions.yaml"


@lru_cache
def get_settings() -> Settings:
    return Settings()
