"""Tests for the prompt builder, its Jinja templates, and `parse_llm_output`."""

import pytest

from app.core.errors import DomainError
from app.core.prompting import Prompt, PromptBuilder, PromptParseError, parse_llm_output

DDL = "CREATE TABLE orders (\n  id BIGINT NOT NULL\n);"
QUESTION = "How many orders were placed?"

EXAMPLES = [
    {
        "id": "q01",
        "question": "How many customers do we have?",
        "sql": "SELECT count(*) FROM customers",
    },
    {"id": "q02", "question": "List all categories.", "sql": "SELECT id, name FROM categories"},
    {"id": "q03", "question": "List all suppliers.", "sql": "SELECT id, name FROM suppliers"},
    {
        "id": "q04",
        "question": "List all payment methods.",
        "sql": "SELECT DISTINCT method FROM payments",
    },
    {"id": "q05", "question": "List all reviews.", "sql": "SELECT id FROM reviews"},
]


# --------------------------------------------------------------------------
# Prompt / PromptBuilder.generate
# --------------------------------------------------------------------------


def test_prompt_is_a_frozen_dataclass_with_a_default_version() -> None:
    prompt = Prompt(system="s", user="u")
    assert prompt.version == "v1"
    with pytest.raises(Exception):  # noqa: B017 -- frozen dataclass raises FrozenInstanceError
        prompt.system = "changed"  # type: ignore[misc]


def test_generate_contains_ddl_question_and_key_rules() -> None:
    prompt = PromptBuilder().generate(QUESTION, DDL)
    full = prompt.system + "\n" + prompt.user

    assert DDL in full
    assert QUESTION in full
    assert "Postgres" in full
    assert "only SELECT" in full


def test_generate_carries_the_builder_version() -> None:
    prompt = PromptBuilder(version="v2").generate(QUESTION, DDL)
    assert prompt.version == "v2"


def test_generate_user_prompt_has_schema_and_question_sections() -> None:
    prompt = PromptBuilder().generate(QUESTION, DDL)
    assert "### Schema" in prompt.user
    assert "### Question" in prompt.user
    assert DDL in prompt.user
    assert QUESTION in prompt.user


def test_generate_with_no_examples_has_no_examples_section() -> None:
    prompt = PromptBuilder().generate(QUESTION, DDL)
    assert "Examples:" not in prompt.system


def test_generate_renders_up_to_three_few_shot_examples() -> None:
    prompt = PromptBuilder(examples=EXAMPLES[:2]).generate(QUESTION, DDL)
    assert prompt.system.count("Q: ") == 2
    for ex in EXAMPLES[:2]:
        assert ex["question"] in prompt.system
        assert ex["sql"] in prompt.system


def test_generate_caps_examples_at_three_even_if_more_are_supplied() -> None:
    prompt = PromptBuilder(examples=EXAMPLES).generate(QUESTION, DDL)
    assert prompt.system.count("Q: ") == 3
    for ex in EXAMPLES[:3]:
        assert ex["question"] in prompt.system
    for ex in EXAMPLES[3:]:
        assert ex["question"] not in prompt.system


def test_generate_example_explanation_defaults_to_example() -> None:
    prompt = PromptBuilder(examples=[EXAMPLES[0]]).generate(QUESTION, DDL)
    assert '"explanation": "Example."' in prompt.system


def test_generate_example_explanation_honours_explicit_value() -> None:
    ex = {**EXAMPLES[0], "explanation": "Counts every customer row."}
    prompt = PromptBuilder(examples=[ex]).generate(QUESTION, DDL)
    assert '"explanation": "Counts every customer row."' in prompt.system


def test_generate_mentions_order_items_revenue_rule() -> None:
    prompt = PromptBuilder().generate(QUESTION, DDL)
    assert "order_items.unit_price" in prompt.system
    assert "order_items.quantity" in prompt.system


# --------------------------------------------------------------------------
# PromptBuilder.repair
# --------------------------------------------------------------------------


def test_repair_contains_schema_question_bad_sql_and_error() -> None:
    bad_sql = "SELECT * FROM ordrs"
    error = 'relation "ordrs" does not exist'
    prompt = PromptBuilder().repair(QUESTION, DDL, bad_sql, error)
    full = prompt.system + "\n" + prompt.user

    assert DDL in full
    assert QUESTION in full
    assert bad_sql in full
    assert error in full
    assert "Postgres" in full
    assert "only SELECT" in full


def test_repair_uses_the_same_system_rules_as_generate() -> None:
    builder = PromptBuilder()
    assert builder.generate(QUESTION, DDL).system == builder.repair(QUESTION, DDL, "x", "y").system


def test_repair_carries_the_builder_version() -> None:
    prompt = PromptBuilder(version="v3").repair(QUESTION, DDL, "x", "y")
    assert prompt.version == "v3"


# --------------------------------------------------------------------------
# parse_llm_output: the four shapes from the brief
# --------------------------------------------------------------------------


def test_parse_bare_json_object() -> None:
    sql, explanation = parse_llm_output('{"sql": "SELECT 1", "explanation": "gives one"}')
    assert sql == "SELECT 1"
    assert explanation == "gives one"


def test_parse_fenced_json() -> None:
    text = '```json\n{"sql": "SELECT 1", "explanation": "gives one"}\n```'
    sql, explanation = parse_llm_output(text)
    assert sql == "SELECT 1"
    assert explanation == "gives one"


def test_parse_fenced_sql() -> None:
    text = "```sql\nSELECT 1\n```"
    sql, explanation = parse_llm_output(text)
    assert sql == "SELECT 1"
    assert explanation == ""


def test_parse_bare_sql_no_structure() -> None:
    sql, explanation = parse_llm_output("SELECT 1")
    assert sql == "SELECT 1"
    assert explanation == ""


def test_parse_prose_with_fenced_sql_block() -> None:
    text = "Here is the SQL:\n```sql\nSELECT 1\n```"
    sql, explanation = parse_llm_output(text)
    assert sql == "SELECT 1"
    assert explanation == ""


def test_parse_strips_trailing_semicolon() -> None:
    sql, _ = parse_llm_output("SELECT 1;")
    assert sql == "SELECT 1"


def test_parse_strips_trailing_semicolon_from_fenced_sql() -> None:
    sql, _ = parse_llm_output("```sql\nSELECT 1;\n```")
    assert sql == "SELECT 1"


# --------------------------------------------------------------------------
# parse_llm_output: extra cases from the task decisions
# --------------------------------------------------------------------------


def test_parse_fenced_json_inside_surrounding_prose() -> None:
    text = (
        "Sure, here you go:\n\n"
        '```json\n{"sql": "SELECT 1", "explanation": "one row"}\n```\n\n'
        "Let me know if you need anything else!"
    )
    sql, explanation = parse_llm_output(text)
    assert sql == "SELECT 1"
    assert explanation == "one row"


def test_parse_json_with_extra_keys_is_ignored() -> None:
    text = '{"sql": "SELECT 1", "explanation": "one row", "confidence": 0.97, "model": "x"}'
    sql, explanation = parse_llm_output(text)
    assert sql == "SELECT 1"
    assert explanation == "one row"


def test_parse_unfenced_json_object_buried_in_prose() -> None:
    text = 'The query you need is {"sql": "SELECT 1", "explanation": "one row"} -- enjoy.'
    sql, explanation = parse_llm_output(text)
    assert sql == "SELECT 1"
    assert explanation == "one row"


def test_parse_does_not_mangle_a_semicolon_inside_a_string_literal() -> None:
    text = '{"sql": "SELECT \';\' AS x;", "explanation": "literal semicolon"}'
    sql, _ = parse_llm_output(text)
    assert sql == "SELECT ';' AS x"


def test_parse_does_not_mangle_a_semicolon_inside_a_bare_sql_string_literal() -> None:
    sql, _ = parse_llm_output("SELECT ';' AS x;")
    assert sql == "SELECT ';' AS x"


def test_parse_untagged_fence_is_still_found() -> None:
    sql, explanation = parse_llm_output("```\nSELECT 1\n```")
    assert sql == "SELECT 1"
    assert explanation == ""


# --------------------------------------------------------------------------
# parse_llm_output: errors
# --------------------------------------------------------------------------


def test_parse_empty_sql_raises_prompt_parse_error() -> None:
    with pytest.raises(PromptParseError):
        parse_llm_output("")


def test_parse_empty_sql_in_json_raises_prompt_parse_error() -> None:
    with pytest.raises(PromptParseError):
        parse_llm_output('{"sql": "", "explanation": "nothing"}')


def test_parse_whitespace_only_raises_prompt_parse_error() -> None:
    with pytest.raises(PromptParseError):
        parse_llm_output("   ;  ")


def test_prompt_parse_error_is_a_domain_error() -> None:
    assert issubclass(PromptParseError, DomainError)
