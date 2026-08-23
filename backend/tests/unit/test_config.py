from app.config import Settings


def test_defaults_point_at_local_stack(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    s = Settings(_env_file=None)
    assert s.llm_provider == "ollama"
    assert s.target_db_url.startswith("postgresql+asyncpg://readonly")
    assert s.max_rows == 500
