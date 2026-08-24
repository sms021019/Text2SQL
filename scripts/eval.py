"""Run `seed/questions.yaml` through a live `POST /api/v1/query` endpoint and
report accuracy. This is an *eval*, not a test (see PLAN.md D10): LLM output
is non-deterministic, so it prints a report and always exits 0 rather than
asserting and failing a CI run.

Usage (run from `backend/` so the package's own environment is used):

    cd backend && python -m uv run python ../scripts/eval.py
    cd backend && python -m uv run python ../scripts/eval.py --dry-run
    cd backend && python -m uv run python ../scripts/eval.py --limit 5 --out ../eval-results.json

For each question in the eval set this POSTs `{"question": ...}` to
`--api-url` + `/api/v1/query` and records:

* `executable` -- the response's `error` field is null (the pipeline
  produced SQL and ran it successfully, with or without a repair);
* `row_match` -- when the question has an `expect_rows`, whether the
  response's `row_count` equals it (skipped/`None` otherwise);
* `latency_ms` -- wall-clock time for the HTTP round trip;
* `tokens` -- prompt + completion tokens from the response's `usage`;
* `error` -- the pipeline's error string, if any.

A table and two summary lines (`executable: X/N (P%)`, `row_match: Y/M
(Q%)`) are printed, and the full per-question results are written to
`--out` (default `eval-results.json`) as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_QUESTIONS = "../seed/questions.yaml"
DEFAULT_OUT = "eval-results.json"
REQUEST_TIMEOUT_S = 120.0


@dataclass
class QuestionSpec:
    id: str
    question: str
    sql: str | None
    expect_rows: int | None
    tags: list[str]


@dataclass
class EvalResult:
    id: str
    question: str
    executable: bool
    row_match: bool | None
    row_count: int | None
    expect_rows: int | None
    latency_ms: float
    tokens: int
    error: str | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="base URL of the running API")
    parser.add_argument("--questions", default=DEFAULT_QUESTIONS, help="path to questions.yaml")
    parser.add_argument("--out", default=DEFAULT_OUT, help="path to write JSON results")
    parser.add_argument("--limit", type=int, default=None, help="only run the first N questions")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only validate questions.yaml and print the question list; makes no HTTP requests",
    )
    return parser.parse_args(argv)


def resolve_questions_path(raw: str) -> Path:
    """Resolve `raw` against the CWD; if that doesn't exist, fall back to
    resolving it relative to this script's directory (so the default works
    whether invoked from `backend/` per the documented usage, or from the
    repo root)."""
    candidate = Path(raw)
    if candidate.exists():
        return candidate
    fallback = Path(__file__).resolve().parent / raw
    if fallback.exists():
        return fallback
    return candidate  # let the caller raise a clear FileNotFoundError


def load_questions(path: Path) -> list[QuestionSpec]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not data:
        raise ValueError(f"no questions found in {path}")
    specs = []
    for entry in data:
        specs.append(
            QuestionSpec(
                id=entry["id"],
                question=entry["question"],
                sql=entry.get("sql"),
                expect_rows=entry.get("expect_rows"),
                tags=entry.get("tags", []),
            )
        )
    return specs


def run_one(client: httpx.Client, api_url: str, spec: QuestionSpec) -> EvalResult:
    start = time.perf_counter()
    try:
        resp = client.post(f"{api_url}/api/v1/query", json={"question": spec.question})
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
    except httpx.HTTPError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        return EvalResult(
            id=spec.id,
            question=spec.question,
            executable=False,
            row_match=None,
            row_count=None,
            expect_rows=spec.expect_rows,
            latency_ms=latency_ms,
            tokens=0,
            error=f"http: {exc}",
        )
    latency_ms = (time.perf_counter() - start) * 1000

    error = body.get("error")
    row_count = body.get("row_count")
    usage = body.get("usage") or {}
    tokens = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))

    executable = error is None
    row_match: bool | None = None
    if spec.expect_rows is not None:
        row_match = executable and row_count == spec.expect_rows

    return EvalResult(
        id=spec.id,
        question=spec.question,
        executable=executable,
        row_match=row_match,
        row_count=row_count,
        expect_rows=spec.expect_rows,
        latency_ms=latency_ms,
        tokens=tokens,
        error=error,
    )


def print_table(results: list[EvalResult]) -> None:
    header = f"{'id':<6} {'exec':<6} {'rows':<6} {'ms':>8}  {'tokens':>7}  question"
    print(header)
    print("-" * len(header))
    for r in results:
        exec_mark = "ok" if r.executable else "FAIL"
        if r.row_match is None:
            row_mark = "-"
        else:
            row_mark = "ok" if r.row_match else "FAIL"
        question = r.question if len(r.question) <= 60 else r.question[:57] + "..."
        row = f"{r.id:<6} {exec_mark:<6} {row_mark:<6} {r.latency_ms:>8.0f}  {r.tokens:>7}  "
        print(row + question)


def print_summary(results: list[EvalResult]) -> None:
    n = len(results)
    executable = sum(1 for r in results if r.executable)
    row_checked = [r for r in results if r.row_match is not None]
    row_match = sum(1 for r in row_checked if r.row_match)

    exec_pct = (executable / n * 100) if n else 0.0
    print(f"\nexecutable: {executable}/{n} ({exec_pct:.0f}%)")
    if row_checked:
        row_pct = (row_match / len(row_checked) * 100) if row_checked else 0.0
        print(f"row_match: {row_match}/{len(row_checked)} ({row_pct:.0f}%)")
    else:
        print("row_match: 0/0 (n/a)")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    questions_path = resolve_questions_path(args.questions)

    try:
        specs = load_questions(questions_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: could not load questions from {questions_path}: {exc}", file=sys.stderr)
        return 0

    if args.limit is not None:
        specs = specs[: args.limit]

    if args.dry_run:
        print(f"loaded {len(specs)} questions from {questions_path}")
        for spec in specs:
            tags = ",".join(spec.tags) if spec.tags else "-"
            expect = spec.expect_rows if spec.expect_rows is not None else "-"
            print(f"  {spec.id:<6} expect_rows={expect!s:<6} tags=[{tags}]  {spec.question}")
        return 0

    results: list[EvalResult] = []
    with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
        for spec in specs:
            result = run_one(client, args.api_url, spec)
            results.append(result)

    print_table(results)
    print_summary(results)

    out_path = Path(args.out)
    out_path.write_text(
        json.dumps([asdict(r) for r in results], indent=2, default=str), encoding="utf-8"
    )
    print(f"\nwrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
