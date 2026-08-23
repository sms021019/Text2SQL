.PHONY: up down logs test lint seed eval
up:        ; docker compose up --build -d
down:      ; docker compose down -v
logs:      ; docker compose logs -f backend
test:      ; cd backend && uv run pytest -q
lint:      ; cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app/core
seed:      ; docker compose run --rm seed
eval:      ; cd backend && uv run python ../scripts/eval.py
