from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from sra.config import PROJECT_ROOT, settings

SQL_DIR = PROJECT_ROOT / "sql"


def _target_database(dsn: str) -> str:
    info = conninfo_to_dict(dsn)
    dbname = info.get("dbname")
    if not dbname:
        raise ValueError(f"no database name in DSN: {dsn}")
    return str(dbname)


def _maintenance_dsn(dsn: str) -> str:
    info = conninfo_to_dict(dsn)
    info["dbname"] = "postgres"
    return " ".join(f"{k}={v}" for k, v in info.items() if v is not None)


def ensure_database(dsn: str) -> bool:
    """Create the target database if absent. Returns True if it was created."""
    dbname = _target_database(dsn)
    with psycopg.connect(_maintenance_dsn(dsn), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
        ).fetchone()
        if exists:
            return False
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    return True


def ensure_reader_role(conn: psycopg.Connection, readonly_dsn: str) -> None:
    """Create or re-password the read-only login the run_sql tool uses.

    Credentials come from SRA_READONLY_DSN so nothing is hardcoded in sql/.
    """
    info = conninfo_to_dict(readonly_dsn)
    user = info.get("user")
    password = info.get("password")
    if not user or not password:
        raise ValueError("SRA_READONLY_DSN must carry both a user and a password")

    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)
    ).fetchone()
    statement = (
        "ALTER ROLE {} WITH LOGIN PASSWORD {}"
        if exists
        else ("CREATE ROLE {} WITH LOGIN PASSWORD {}")
    )
    conn.execute(
        sql.SQL(statement).format(sql.Identifier(str(user)), sql.Literal(str(password)))
    )


def apply_migrations() -> list[str]:
    cfg = settings()
    ensure_database(cfg.dsn)
    applied: list[str] = []
    files = sorted(SQL_DIR.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"no migrations in {SQL_DIR}")
    with psycopg.connect(cfg.dsn) as conn:
        for path in files:
            # The reader role must exist before the file that grants to it.
            if path.name.startswith("002"):
                ensure_reader_role(conn, cfg.readonly_dsn)
            conn.execute(_read(path))
            applied.append(path.name)
        conn.commit()
    return applied


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")
