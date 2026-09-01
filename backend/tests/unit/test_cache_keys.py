"""Tests for `app.cache.keys`: question normalisation and cache-key derivation.

These are pure functions -- no Redis involved -- so they live in `unit/`
alongside `test_guard.py` and friends.
"""

from app.cache.keys import normalise_question, result_cache_key, schema_cache_key, sql_cache_key

# --------------------------------------------------------------------------
# normalise_question
# --------------------------------------------------------------------------


def test_normalise_lowercases_collapses_whitespace_and_strips_trailing_punct() -> None:
    assert normalise_question("How many ORDERS?  ") == "how many orders"


def test_normalise_collapses_internal_whitespace() -> None:
    assert normalise_question("how   many\torders\n") == "how many orders"


def test_normalise_strips_multiple_trailing_punct_chars() -> None:
    assert normalise_question("how many orders?!.;") == "how many orders"


def test_normalise_strips_trailing_punct_then_whitespace_iteratively() -> None:
    # trailing punctuation followed by more whitespace/punct should all go
    assert normalise_question("how many orders ?  ") == "how many orders"


def test_normalise_leaves_internal_punctuation_alone() -> None:
    assert normalise_question("what's the status?") == "what's the status"


def test_normalise_empty_string() -> None:
    assert normalise_question("") == ""


def test_normalise_only_punctuation() -> None:
    assert normalise_question("???") == ""


def test_normalise_is_idempotent() -> None:
    q = "How many ORDERS?  "
    once = normalise_question(q)
    assert normalise_question(once) == once


# --------------------------------------------------------------------------
# sql_cache_key
# --------------------------------------------------------------------------


def _key(**overrides: str) -> str:
    base = {
        "schema_version": "v1",
        "model": "qwen2.5-coder:7b",
        "prompt_version": "v1",
        "question": "How many orders?",
    }
    base.update(overrides)
    return sql_cache_key(**base)  # type: ignore[arg-type]


def test_sql_cache_key_has_expected_prefix_and_length() -> None:
    key = _key()
    assert key.startswith("sql:")
    digest = key.removeprefix("sql:")
    assert len(digest) == 32
    assert all(c in "0123456789abcdef" for c in digest)


def test_sql_cache_key_is_stable_for_same_inputs() -> None:
    assert _key() == _key()


def test_sql_cache_key_stable_across_question_formatting_variants() -> None:
    # normalisation happens inside sql_cache_key, so these should collide
    assert _key(question="How many orders?") == _key(question="how many orders")
    assert _key(question="how many orders  ") == _key(question="how many orders")


def test_sql_cache_key_changes_when_schema_version_changes() -> None:
    assert _key(schema_version="v1") != _key(schema_version="v2")


def test_sql_cache_key_changes_when_model_changes() -> None:
    assert _key(model="a") != _key(model="b")


def test_sql_cache_key_changes_when_prompt_version_changes() -> None:
    assert _key(prompt_version="v1") != _key(prompt_version="v2")


def test_sql_cache_key_changes_when_question_changes() -> None:
    assert _key(question="How many orders?") != _key(question="How many customers?")


# --------------------------------------------------------------------------
# result_cache_key / schema_cache_key
# --------------------------------------------------------------------------


def test_result_cache_key_reuses_the_sql_hash() -> None:
    sql_key = _key()
    digest = sql_key.removeprefix("sql:")
    assert result_cache_key(sql_key=sql_key) == f"res:{digest}"


def test_schema_cache_key_format() -> None:
    assert schema_cache_key("v3") == "schema:v3"
