.PHONY: up up-ollama down logs test lint eval fe-dev fe-test
up:        ; docker compose up --build -d
up-ollama: ; docker compose --profile ollama up --build -d
down:      ; docker compose down -v
logs:      ; docker compose logs -f backend
test:      ; cd backend && uv run pytest -q
lint:      ; cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app
eval:      ; cd backend && uv run python ../scripts/eval.py
fe-dev:    ; cd frontend && npm run dev
fe-test:   ; cd frontend && npm test
