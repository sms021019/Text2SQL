# Text2SQL frontend

Vite + React + TypeScript UI for the Text2SQL backend (`../backend`).

## Development

```sh
npm install
npm run dev        # http://localhost:5173, proxies /api to http://localhost:8000
npx vitest run      # unit tests
npx tsc --noEmit     # typecheck
npm run build        # production build to dist/
```

`VITE_API_URL` overrides the API base (defaults to `/api`, relying on the
dev server / nginx proxy).

## Docker

Built and served by `frontend/Dockerfile` (nginx) as the `frontend` service
in the repo's `docker-compose.yml`, on port 5173.
