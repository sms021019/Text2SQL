.PHONY: up up-ollama down logs test lint eval load-test fe-dev fe-test

# Where scripts/load_test.js sends its requests. `--network host` reaches
# the backend's published 127.0.0.1:8000 on Linux; on Docker Desktop
# (macOS/Windows) it does not, so point the k6 container at the host gateway
# instead:  API_URL=http://host.docker.internal:8000 make load-test
API_URL ?= http://127.0.0.1:8000

# Pinned (not `grafana/k6:latest`) so the demo's numbers come from the same
# k6 every time -- same tag `scripts/load_test.js`'s header documents.
K6_IMAGE ?= grafana/k6:0.55.0

up:        ; docker compose up --build -d
up-ollama: ; docker compose --profile ollama up --build -d
down:      ; docker compose down -v
logs:      ; docker compose logs -f backend
test:      ; cd backend && python -m uv run pytest -q
lint:      ; cd backend && python -m uv run ruff check . && python -m uv run ruff format --check . && python -m uv run mypy app
eval:      ; cd backend && python -m uv run python ../scripts/eval.py
load-test: ; docker run --rm -i --network host -e API_URL=$(API_URL) $(K6_IMAGE) run - <scripts/load_test.js
fe-dev:    ; cd frontend && npm run dev
fe-test:   ; cd frontend && npm test
