# Stage 1: generate the deterministic seed CSVs.
FROM python:3.12-slim AS csvgen
RUN pip install --no-cache-dir faker
COPY generate.py /seed/generate.py
RUN python /seed/generate.py

# Stage 2: postgres image pre-loaded with schema, roles and seed data.
FROM postgres:16-alpine
COPY --from=csvgen /seed/data /seed/data
COPY schema.sql roles.sql /seed/
COPY init.sh /docker-entrypoint-initdb.d/10-init.sh
RUN chmod +x /docker-entrypoint-initdb.d/10-init.sh
