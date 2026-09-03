from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from sra.config import settings
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


def run_sql(sql: str) -> SqlResult:
    """Execute one read-only query as the least-privileged role.

    Three independent guards: the role holds no write grants, the transaction is
    opened READ ONLY, and only a single SELECT/WITH statement is accepted.
    """
    try:
        query = assert_single_read_query(sql)
    except UnsafeSqlError as exc:
        return SqlResult(sql=sql, error=str(exc))

    limit = settings().sql_row_limit
    try:
        with psycopg.connect(settings().readonly_dsn, row_factory=dict_row) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                # LOCAL scopes the timeout to this transaction, so it cannot be
                # left raised for a later query on a pooled connection.
                cur.execute("SET LOCAL statement_timeout = '5s'")
                cur.execute(query)
                if cur.description is None:
                    return SqlResult(sql=query, error="query returned no result set")
                columns = [c.name for c in cur.description]
                fetched = cur.fetchmany(limit + 1)
    except psycopg.Error as exc:
        return SqlResult(sql=query, error=str(exc).strip())

    truncated = len(fetched) > limit
    rows = [{col: _serialize(row[col]) for col in columns} for row in fetched[:limit]]
    return SqlResult(sql=query, columns=columns, rows=rows, truncated=truncated)
