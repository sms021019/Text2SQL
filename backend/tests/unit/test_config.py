from app.config import Settings


def test_defaults_point_at_local_stack(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    s = Settings(_env_file=None)
    assert s.llm_provider == "ollama"
    assert s.target_db_url.startswith("postgresql+asyncpg://readonly")
    assert s.max_rows == 500
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.cache_enabled is True
    assert s.sql_cache_ttl_s == 86400
    assert s.result_cache_ttl_s == 300
    assert s.schema_cache_ttl_s == 604800
