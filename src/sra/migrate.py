from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from sra.config import PROJECT_ROOT, settings

SQL_DIR = PROJECT_ROOT / "sql"

# The sql/ files grant to and revoke from this role by name.
READER_ROLE = "sra_reader"

# Arbitrary constant naming this app's migration lock among advisory locks.
_LOCK_KEY = 0x5352414D  # "SRAM"

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _target_database(dsn: str) -> str:
    info = conninfo_to_dict(dsn)
    dbname = info.get("dbname")
    if not dbname:
        # Not echoing the DSN: it carries the password.
        raise ValueError("no database name in SRA_DSN")
    return str(dbname)


def _maintenance_dsn(dsn: str) -> str:
    # make_conninfo quotes and escapes, so passwords or options containing
    # spaces, quotes or backslashes survive the rebuild.
    return make_conninfo(dsn, dbname="postgres")


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
    if user != READER_ROLE:
        # sql/002 and later grant to and revoke from this exact role name.
        raise ValueError(
            f"SRA_READONLY_DSN user must be {READER_ROLE}: the sql/ files "
            f"hard-code that role (got {user})"
        )

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


def _record_applied(conn: psycopg.Connection, filename: str) -> None:
    conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (filename,))


def _apply_files(
    conn: psycopg.Connection, files: list[Path], readonly_dsn: str
) -> list[str]:
    """Apply every file not yet in the ledger; the caller holds the lock.

    `conn` must be in autocommit mode so each file gets its own explicit
    transaction: a failure rolls back that file and its ledger row together
    and leaves earlier files recorded.
    """
    conn.execute(_LEDGER_DDL)
    # Always run, not only when 002 is pending: it re-applies the password
    # from SRA_READONLY_DSN, and it must precede every file that grants to or
    # revokes from the role.
    ensure_reader_role(conn, readonly_dsn)
    done = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}
    applied: list[str] = []
    for path in files:
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute(_read(path))
            _record_applied(conn, path.name)
        applied.append(path.name)
    return applied


def apply_migrations() -> list[str]:
    """Apply pending sql/ files in order; returns the names actually applied.

    Databases that predate the ledger have none, and every file is
    idempotent, so the first run simply re-applies and records all of them.
    """
    cfg = settings()
    ensure_database(cfg.dsn)
    files = sorted(SQL_DIR.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"no migrations in {SQL_DIR}")
    with psycopg.connect(cfg.dsn, autocommit=True) as conn:
        # Session-level lock: concurrent `sra migrate` runs queue here instead
        # of racing on DDL and the ledger. It dies with the connection.
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            return _apply_files(conn, files, cfg.readonly_dsn)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")
