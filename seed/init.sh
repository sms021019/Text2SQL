#!/usr/bin/env bash
set -euo pipefail
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" <<-SQL
  CREATE DATABASE app;
  CREATE ROLE app LOGIN PASSWORD 'app';
  GRANT ALL PRIVILEGES ON DATABASE app TO app;
  CREATE DATABASE target;
SQL
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d target -f /seed/schema.sql
for t in customers addresses categories suppliers products inventory orders order_items payments shipments reviews; do
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d target -c "\copy $t FROM '/seed/data/$t.csv' CSV HEADER"
done
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -f /seed/roles.sql
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d app -c "GRANT ALL ON SCHEMA public TO app;"
