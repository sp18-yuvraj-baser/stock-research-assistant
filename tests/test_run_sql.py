"""run_sql against a fake connection: no database is needed or touched."""

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from sra.tools.run_sql import SqlResult, run_sql


class _Cursor:
    def __init__(self, names: list[str], rows: list[tuple[Any, ...]]) -> None:
        self.description = [SimpleNamespace(name=n) for n in names]
        self._rows = rows
        self.executed: list[tuple[str, Any]] = []
        self.fetch_sizes: list[int] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        pass

    def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))

    def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        self.fetch_sizes.append(size)
        return self._rows[:size]


class _Conn:
    def __init__(self, cursor: _Cursor) -> None:
        self.cursor_obj = cursor
        self.read_only = False
        self.cursor_kwargs: dict[str, Any] = {}
        self.statements: list[str] = []
        self.seen: dict[str, Any] = {}

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def cursor(self, **kwargs: Any) -> _Cursor:
        self.cursor_kwargs = kwargs
        return self.cursor_obj


def _install(
    monkeypatch: pytest.MonkeyPatch, cursor: _Cursor, *, limit: int = 200
) -> _Conn:
    conn = _Conn(cursor)

    @contextmanager
    def fake_connect(*, readonly: bool = False) -> Iterator[_Conn]:
        conn.seen["readonly"] = readonly
        yield conn

    monkeypatch.setattr("sra.tools.run_sql.connect", fake_connect)
    monkeypatch.setattr(
        "sra.tools.run_sql.settings", lambda: SimpleNamespace(sql_row_limit=limit)
    )
    return conn


def test_duplicate_column_names_are_kept_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor(["pe", "pe", "pe", "name"], [(10, 20, 30, "TCS")])
    _install(monkeypatch, cursor)
    result = run_sql("SELECT a.pe, b.pe, c.pe, name FROM t")
    assert result.columns == ["pe", "pe_2", "pe_3", "name"]
    assert result.rows == [{"pe": 10, "pe_2": 20, "pe_3": 30, "name": "TCS"}]
    # Both values reach the model; neither overwrote the other.
    assert result.to_text().splitlines()[:2] == [
        "pe | pe_2 | pe_3 | name",
        "10 | 20 | 30 | TCS",
    ]


def test_generated_name_does_not_collide_with_a_real_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor(["x", "x", "x_2"], [(1, 2, 3)])
    _install(monkeypatch, cursor)
    result = run_sql("SELECT 1 AS x, 2 AS x, 3 AS x_2")
    assert len(set(result.columns)) == 3
    assert [list(r.values()) for r in result.rows] == [[1, 2, 3]]


def test_row_limit_is_enforced_by_a_server_side_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor(["n"], [(i,) for i in range(1000)])
    conn = _install(monkeypatch, cursor, limit=3)
    result = run_sql("SELECT n FROM big")
    assert conn.cursor_kwargs["name"]  # named == server-side
    assert cursor.fetch_sizes == [4]  # limit + 1, never the whole result
    assert result.truncated is True
    assert [r["n"] for r in result.rows] == [0, 1, 2]
    assert "truncated" in result.to_text()


def test_result_at_exactly_the_limit_is_not_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _Cursor(["n"], [(1,), (2,), (3,)]), limit=3)
    result = run_sql("SELECT n FROM t")
    assert result.truncated is False
    assert len(result.rows) == 3


def test_runs_read_only_with_a_timeout_and_binds_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor(["n"], [(1,)])
    conn = _install(monkeypatch, cursor)
    run_sql("SELECT n FROM t WHERE k = %(k)s;", {"k": "TCS"})
    assert conn.seen["readonly"] is True
    assert conn.read_only is True
    assert conn.statements == ["SET LOCAL statement_timeout = '5s'"]
    assert cursor.executed == [("SELECT n FROM t WHERE k = %(k)s", {"k": "TCS"})]


def test_numeric_and_null_values_render_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _Cursor(["v", "w"], [(Decimal("12.500"), None)]))
    result = run_sql("SELECT v, w FROM t")
    assert result.rows == [{"v": "12.5", "w": None}]
    assert "12.5 | NULL" in result.to_text()


def test_unsafe_sql_is_rejected_before_any_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**_kw: Any) -> Any:
        raise AssertionError("must not connect")

    monkeypatch.setattr("sra.tools.run_sql.connect", boom)
    result = run_sql("SELECT 1; DROP TABLE x")
    assert isinstance(result, SqlResult)
    assert result.error and "one statement" in result.error


def test_database_errors_become_an_error_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Failing:
        def __enter__(self) -> Any:
            raise psycopg.OperationalError("connection refused")

        def __exit__(self, *_exc: object) -> None:
            pass

    def failing(*, readonly: bool = False) -> _Failing:
        return _Failing()

    monkeypatch.setattr("sra.tools.run_sql.connect", failing)
    monkeypatch.setattr(
        "sra.tools.run_sql.settings", lambda: SimpleNamespace(sql_row_limit=10)
    )
    result = run_sql("SELECT 1")
    assert result.error == "connection refused"
    assert result.to_text() == "ERROR: connection refused"


def test_db_connect_sets_timeout_application_name_and_picks_the_role_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from psycopg.rows import dict_row

    from sra.db import CONNECT_TIMEOUT_SECONDS, connect

    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeConnection:
        def __enter__(self) -> "_FakeConnection":
            return self

        def __exit__(self, *_exc: object) -> None:
            pass

    def fake_connect(dsn: str, **kwargs: Any) -> _FakeConnection:
        calls.append((dsn, kwargs))
        return _FakeConnection()

    monkeypatch.setattr("sra.db.psycopg.connect", fake_connect)
    monkeypatch.setattr(
        "sra.db.settings", lambda: SimpleNamespace(dsn="owner", readonly_dsn="reader")
    )
    with connect():
        pass
    with connect(readonly=True):
        pass

    assert [c[0] for c in calls] == ["owner", "reader"]
    for _dsn, kwargs in calls:
        assert kwargs["row_factory"] is dict_row
        assert kwargs["connect_timeout"] == CONNECT_TIMEOUT_SECONDS
        assert kwargs["application_name"]
    assert calls[0][1]["application_name"] != calls[1][1]["application_name"]
