"""Prompt construction for SQL generation and repair, plus LLM output parsing.

`PromptBuilder` renders Jinja templates (see `templates/`) into a `Prompt`
(system + user text) for two use cases:

* `generate()` -- turn a natural-language question and a schema DDL excerpt
  into a first-attempt prompt, optionally primed with up to
  :data:`MAX_EXAMPLES` few-shot examples (`{question, sql}` dicts, e.g. from
  `seed/questions.yaml` -- the builder does not load that file itself, a
  caller such as the generation pipeline does).
* `repair()` -- turn a failed SQL statement and the database's error message
  into a follow-up prompt asking for a corrected query, using the same
  system rules as `generate()`.

`parse_llm_output()` is the other half: it turns raw LLM text back into a
`(sql, explanation)` pair, tolerating the several shapes a model tends to
actually return (a bare JSON object, a fenced ```json``` or ```sql``` block
possibly wrapped in prose, an unfenced `{...}` object buried in prose, or
plain SQL with no structure at all).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.core.errors import DomainError

__all__ = ["MAX_EXAMPLES", "Prompt", "PromptBuilder", "PromptParseError", "parse_llm_output"]

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=StrictUndefined,
)

#: At most this many few-shot examples are ever rendered into the system
#: prompt, regardless of how many the caller supplies.
MAX_EXAMPLES = 3


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    version: str = "v1"


class PromptBuilder:
    """Renders `generate`/`repair` prompts for a fixed template `version` and
    a fixed pool of few-shot examples.

    `examples` items must be dicts with at least `question` and `sql` keys
    (the shape used by `seed/questions.yaml`); an `explanation` key is used
    if present, otherwise each example is annotated "Example.". Only the
    first `MAX_EXAMPLES` are ever used.
    """

    def __init__(self, version: str = "v1", examples: list[dict[str, Any]] | None = None) -> None:
        self.version = version
        self._examples = list(examples[:MAX_EXAMPLES]) if examples else []

    def generate(self, question: str, ddl: str) -> Prompt:
        system = _ENV.get_template("generate_system.j2").render(
            example_blocks=self._example_blocks()
        )
        user = _ENV.get_template("generate_user.j2").render(ddl=ddl, question=question)
        return Prompt(system=system, user=user, version=self.version)

    def repair(self, question: str, ddl: str, bad_sql: str, error: str) -> Prompt:
        system = _ENV.get_template("repair_system.j2").render(example_blocks=self._example_blocks())
        user = _ENV.get_template("repair_user.j2").render(
            ddl=ddl, question=question, bad_sql=bad_sql, error=error
        )
        return Prompt(system=system, user=user, version=self.version)

    def _example_blocks(self) -> list[str]:
        blocks = []
        for ex in self._examples:
            payload = {"sql": ex["sql"], "explanation": ex.get("explanation", "Example.")}
            blocks.append(f"Q: {ex['question']}\nA: {json.dumps(payload)}")
        return blocks


class PromptParseError(DomainError):
    """Raised when `parse_llm_output` cannot extract a non-empty SQL string
    from the model's response."""


#: Matches the first fenced code block, tolerating a `json`/`sql` language
#: tag (case-insensitive) but not any other word -- an untagged fence such as
#: ` ```SELECT 1``` ` must not have its content mistaken for a language tag.
_FENCE_RE = re.compile(r"```(?:json|sql)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_llm_output(text: str) -> tuple[str, str]:
    """Extract `(sql, explanation)` from raw LLM response text.

    Tried in order: the whole text as a JSON object; the body of the first
    fenced code block (as JSON, else as bare SQL); the first balanced
    `{...}` object found anywhere in the text (as JSON); otherwise the whole
    text is treated as bare SQL with an empty explanation.

    A trailing `;` (and surrounding whitespace) is stripped from the SQL;
    semicolons elsewhere -- e.g. inside a string literal -- are left alone.

    Raises:
        PromptParseError: if the resulting SQL is empty.
    """
    sql, explanation = _extract(text.strip())

    sql = sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()
    if not sql:
        raise PromptParseError("LLM output did not contain a SQL statement")
    return sql, explanation


def _extract(text: str) -> tuple[str, str]:
    obj = _try_json_object(text)
    if obj is not None:
        return _from_obj(obj)

    fence = _FENCE_RE.search(text)
    if fence is not None:
        body = fence.group(1).strip()
        obj = _try_json_object(body)
        if obj is not None:
            return _from_obj(obj)
        return body, ""

    balanced = _find_balanced_json(text)
    if balanced is not None:
        obj = _try_json_object(balanced)
        if obj is not None:
            return _from_obj(obj)

    return text, ""


def _try_json_object(text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _from_obj(obj: dict[str, Any]) -> tuple[str, str]:
    sql = obj.get("sql")
    explanation = obj.get("explanation")
    return (
        sql if isinstance(sql, str) else "",
        explanation if isinstance(explanation, str) else "",
    )


def _find_balanced_json(text: str) -> str | None:
    """Return the first balanced `{...}` substring of `text`, if any.

    Scans for a matching close brace while tracking string/escape state so a
    `}` or `{` inside a quoted string does not throw off the brace count.
    Retries from the next `{` if a candidate never balances.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None
