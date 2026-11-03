import hashlib
import os
from importlib.resources import files

import psycopg
from psycopg.rows import dict_row


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set DATABASE_URL to a PostgreSQL connection string")
    return url


def connect(url: str):
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=5)


def migrate(url: str):
    """Transactional, checksummed SQL migrations; safe for concurrent startup commands."""
    with connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(72840123)")
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            name text PRIMARY KEY, checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
        for path in sorted(files("knowledge").joinpath("migrations").iterdir(), key=lambda p: p.name):
            if not path.name.endswith(".sql"):
                continue
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            previous = conn.execute("SELECT checksum FROM schema_migrations WHERE name=%s", (path.name,)).fetchone()
            if previous:
                if previous["checksum"] != checksum:
                    raise RuntimeError(f"Applied migration changed: {path.name}")
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (name,checksum) VALUES (%s,%s)", (path.name, checksum))


if __name__ == "__main__":
    migrate(database_url())
    print("Knowledge database is up to date.")
