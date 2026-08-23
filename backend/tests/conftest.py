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
