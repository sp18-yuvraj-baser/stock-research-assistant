from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from sra import migrate

READONLY_DSN = "postgresql://sra_reader:pw@localhost/sra"


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(self._rows)


class _FakeConn:
    """Records statements and transaction boundaries; the ledger is a set."""

    def __init__(self, applied: set[str], fail_on: str | None = None) -> None:
        self.ledger = set(applied)
        self.fail_on = fail_on
        self.log: list[str] = []
        self._pending: set[str] = set()
        self._in_tx = False

    def execute(self, query: str, params: Any = None) -> _Result:
        text = " ".join(str(query).split())
        if text.startswith("SELECT filename FROM schema_migrations"):
            return _Result([(f,) for f in sorted(self.ledger)])
        if text.startswith("SELECT 1 FROM pg_roles"):
            return _Result([])
        if text.startswith("INSERT INTO schema_migrations"):
            assert self._in_tx, "ledger row must share the file's transaction"
            self._pending.add(params[0])
            self.log.append(f"ledger {params[0]}")
            return _Result([])
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("boom")
        if "CREATE ROLE" in text or "ALTER ROLE" in text:
            self.log.append("role")
        else:
            self.log.append(f"sql {text[:40]}")
        return _Result([])

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._in_tx = True
        self._pending = set()
        try:
            yield
        except Exception:
            self.log.append("rollback")
            raise
        else:
            self.ledger |= self._pending
        finally:
            self._in_tx = False


def _files(tmp_path: Path, names: list[str], body: str = "SELECT 1;") -> list[Path]:
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_text(f"-- {name}\n{body}", encoding="utf-8")
        paths.append(path)
    return paths


def test_applies_only_unrecorded_files_and_records_them(tmp_path: Path) -> None:
    conn = _FakeConn(applied={"001_a.sql"})
    files = _files(tmp_path, ["001_a.sql", "002_b.sql", "003_c.sql"])
    applied = migrate._apply_files(conn, files, READONLY_DSN)  # type: ignore[arg-type]
    assert applied == ["002_b.sql", "003_c.sql"]
    assert conn.ledger == {"001_a.sql", "002_b.sql", "003_c.sql"}


def test_second_run_applies_nothing(tmp_path: Path) -> None:
    conn = _FakeConn(applied=set())
    files = _files(tmp_path, ["001_a.sql", "002_b.sql"])
    assert migrate._apply_files(conn, files, READONLY_DSN) == ["001_a.sql", "002_b.sql"]  # type: ignore[arg-type]
    assert migrate._apply_files(conn, files, READONLY_DSN) == []  # type: ignore[arg-type]


def test_reader_role_is_ensured_before_any_file(tmp_path: Path) -> None:
    conn = _FakeConn(applied=set())
    migrate._apply_files(conn, _files(tmp_path, ["001_a.sql"]), READONLY_DSN)  # type: ignore[arg-type]
    first_file = next(i for i, e in enumerate(conn.log) if e.startswith("sql -- 001"))
    assert conn.log.index("role") < first_file


def test_failed_file_rolls_back_and_is_not_recorded(tmp_path: Path) -> None:
    conn = _FakeConn(applied=set(), fail_on="BAD")
    files = _files(tmp_path, ["001_a.sql"]) + _files(tmp_path, ["002_b.sql"], "BAD;")
    files += _files(tmp_path, ["003_c.sql"])
    with pytest.raises(RuntimeError):
        migrate._apply_files(conn, files, READONLY_DSN)  # type: ignore[arg-type]
    # The earlier file stays recorded; the failing one and later ones do not.
    assert conn.ledger == {"001_a.sql"}
    assert "rollback" in conn.log


def test_maintenance_dsn_quotes_awkward_values() -> None:
    dsn = r"host=db user=u password='a b\'c\\d' dbname=sra"
    rebuilt = conninfo_to_dict(migrate._maintenance_dsn(dsn))
    assert rebuilt["dbname"] == "postgres"
    assert rebuilt["password"] == "a b'c\\d"
    assert rebuilt["host"] == "db"


def test_target_database_error_does_not_echo_the_password() -> None:
    with pytest.raises(ValueError) as exc:
        migrate._target_database("host=db password=secret")
    assert "secret" not in str(exc.value)


def test_apply_migrations_holds_the_lock_and_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statements: list[str] = []

    class _Cfg:
        dsn = "postgresql://localhost/sra"
        readonly_dsn = READONLY_DSN

    class _Conn:
        def __enter__(self) -> "_Conn":
            return self

        def __exit__(self, *_exc: object) -> None:
            pass

        def execute(self, query: str, params: Any = None) -> _Result:
            statements.append(" ".join(str(query).split()))
            return _Result([])

    seen: dict[str, Any] = {}

    def fake_connect(dsn: str, **kwargs: Any) -> _Conn:
        seen.update(kwargs)
        return _Conn()

    def fake_apply(conn: object, files: list[Path], readonly_dsn: str) -> list[str]:
        statements.append("apply")
        return [f.name for f in files]

    _files(tmp_path, ["001_a.sql"])
    monkeypatch.setattr(migrate, "SQL_DIR", tmp_path)
    monkeypatch.setattr(migrate, "settings", lambda: _Cfg())
    monkeypatch.setattr(migrate, "ensure_database", lambda _dsn: False)
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr(migrate, "_apply_files", fake_apply)

    assert migrate.apply_migrations() == ["001_a.sql"]
    assert seen["autocommit"] is True
    assert statements == [
        "SELECT pg_advisory_lock(%s)",
        "apply",
        "SELECT pg_advisory_unlock(%s)",
    ]


def test_every_sql_file_is_sec_free_where_superseded() -> None:
    # 001 and 003 are kept only so filenames stay stable; they must not
    # recreate the dropped SEC tables on a fresh database.
    for name in ("001_init.sql", "003_filing_primary_document.sql"):
        text = (migrate.SQL_DIR / name).read_text(encoding="utf-8").upper()
        assert "CREATE TABLE" not in text and "ALTER TABLE" not in text


def test_reader_role_must_be_sra_reader() -> None:
    conn = _FakeConn(applied=set())
    with pytest.raises(ValueError) as exc:
        migrate.ensure_reader_role(conn, "postgresql://other:pw@localhost/sra")  # type: ignore[arg-type]
    assert "sra_reader" in str(exc.value)
    assert "pw" not in str(exc.value)
    assert conn.log == []
    migrate.ensure_reader_role(conn, READONLY_DSN)  # type: ignore[arg-type]
    assert conn.log == ["role"]
