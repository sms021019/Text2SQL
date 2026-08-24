import pathlib
import subprocess
import sys

import pytest
from testcontainers.postgres import PostgresContainer

SEED = pathlib.Path(__file__).resolve().parents[2] / "seed"


@pytest.fixture(scope="session")
def pg():
    with PostgresContainer(
        "postgres:16-alpine", username="postgres", password="postgres", dbname="postgres"
    ) as c:
        yield c


@pytest.fixture(scope="session")
def target_db_url(pg):  # sync psycopg url for seeding
    import psycopg

    admin = pg.get_connection_url(driver=None)
    subprocess.run([sys.executable, str(SEED / "generate.py")], check=True)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("CREATE DATABASE target")
        conn.execute("CREATE DATABASE app")
        conn.execute("CREATE ROLE app LOGIN PASSWORD 'app'")
        conn.execute("GRANT ALL PRIVILEGES ON DATABASE app TO app")
        # roles.sql (executed below, against the target-db connection) now
        # revokes CONNECT on `app` from PUBLIC -- mirror that here too, and
        # keep an explicit CONNECT grant to `app` itself so its own
        # connection (used by the app_db_url fixture) still works.
        conn.execute("GRANT CONNECT ON DATABASE app TO app")
    a = admin.rsplit("/", 1)[0] + "/app"
    with psycopg.connect(a, autocommit=True) as conn:
        # Postgres 15+ no longer grants CREATE on the public schema to
        # everyone by default, so the database-level grant above isn't
        # enough for `app` to create tables in its own `public` schema.
        # Mirrors `seed/init.sh`, which does the same for the container.
        conn.execute("GRANT ALL ON SCHEMA public TO app")
    # Replace only the trailing dbname segment; `.replace("/postgres", ...)`
    # would also clobber the "postgres" username baked into the URL.
    t = admin.rsplit("/", 1)[0] + "/target"
    with psycopg.connect(t, autocommit=True) as conn:
        conn.execute((SEED / "schema.sql").read_text())
        with conn.cursor() as cur:
            for tbl in [
                "customers",
                "addresses",
                "categories",
                "suppliers",
                "products",
                "inventory",
                "orders",
                "order_items",
                "payments",
                "shipments",
                "reviews",
            ]:
                with (
                    open(SEED / "data" / f"{tbl}.csv") as f,
                    cur.copy(f"COPY {tbl} FROM STDIN CSV HEADER") as cp,
                ):
                    cp.write(f.read())
        conn.execute((SEED / "roles.sql").read_text().replace("\\c target", ""))
    return t


@pytest.fixture(scope="session")
def readonly_async_url(target_db_url):
    host_port = target_db_url.split("@")[1]
    return f"postgresql+asyncpg://readonly:readonly@{host_port}"


@pytest.fixture(scope="session")
def app_db_url(target_db_url):
    # target_db_url ends in "/target"; app's database is named "app".
    host_port = target_db_url.split("@")[1].rsplit("/", 1)[0]
    return f"postgresql+asyncpg://app:app@{host_port}/app"
