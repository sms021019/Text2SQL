# Stage 1: build the static SPA bundle.
FROM node:22-alpine AS builder
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# Stage 2: serve the bundle with nginx, proxying /api/ to the backend
# service so the SPA can call same-origin `/api/...` paths in both dev
# (Vite's proxy, see vite.config.ts) and this production image.
FROM nginx:alpine

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s \
    CMD wget -qO- http://localhost:80/ >/dev/null 2>&1 || exit 1
