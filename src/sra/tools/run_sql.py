from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import tuple_row

from sra.config import settings
from sra.db import connect
from sra.tools.sql_guard import UnsafeSqlError, assert_single_read_query


@dataclass(frozen=True)
class SqlResult:
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None

    def to_text(self) -> str:
        """Render for the model. Values are passed through verbatim, never
        reformatted, so a figure in the answer can be matched back to a row."""
        if self.error:
            return f"ERROR: {self.error}"
        if not self.rows:
            return "0 rows"
        header = " | ".join(self.columns)
        body = "\n".join(
            " | ".join("NULL" if r[c] is None else str(r[c]) for c in self.columns)
            for r in self.rows
        )
        count = len(self.rows)
        plural = "row" if count == 1 else "rows"
        note = (
            f"\n({count} {plural} shown; result was truncated)"
            if self.truncated
            else f"\n({count} {plural})"
        )
        return f"{header}\n{body}{note}"


def _serialize(value: Any) -> Any:
    # NUMERIC stays a string: a float round-trip would silently alter a figure
    # that the answer is supposed to reproduce exactly.
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _unique_names(names: list[str]) -> list[str]:
    """Make column names unique: a repeat becomes x_2, x_3, in column order.

    Rows are dicts keyed by name, so a join selecting a.x and b.x would
    otherwise silently drop one value and show the model a wrong figure.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        candidate, n = name, 1
        while candidate in seen:
            n += 1
            candidate = f"{name}_{n}"
        seen.add(candidate)
        unique.append(candidate)
    return unique


def run_sql(sql: str, params: dict[str, Any] | None = None) -> SqlResult:
    """Execute one read-only query as the least-privileged role.

    Three independent guards: the role holds no write grants, the transaction is
    opened READ ONLY, and only a single SELECT/WITH statement is accepted.
    params binds values for queries written in code; the model never passes any.
    """
    try:
        query = assert_single_read_query(sql)
    except UnsafeSqlError as exc:
        return SqlResult(sql=sql, error=str(exc))

    limit = settings().sql_row_limit
    try:
        with connect(readonly=True) as conn:
            conn.read_only = True
            # LOCAL scopes the timeout to this transaction, so it cannot be
            # left raised for a later query on a pooled connection.
            conn.execute("SET LOCAL statement_timeout = '5s'")
            # A named cursor is server-side: only limit + 1 rows ever cross the
            # wire, however large the result. It also makes Postgres refuse a
            # multi-statement string, a second line of defence behind the guard.
            with conn.cursor(name="sra_run_sql", row_factory=tuple_row) as cur:
                cur.execute(query, params)
                if cur.description is None:
                    return SqlResult(sql=query, error="query returned no result set")
                columns = _unique_names([c.name for c in cur.description])
                fetched = cur.fetchmany(limit + 1)
    except psycopg.Error as exc:
        return SqlResult(sql=query, error=str(exc).strip())

    truncated = len(fetched) > limit
    rows = [
        {col: _serialize(value) for col, value in zip(columns, row, strict=True)}
        for row in fetched[:limit]
    ]
    return SqlResult(sql=query, columns=columns, rows=rows, truncated=truncated)
