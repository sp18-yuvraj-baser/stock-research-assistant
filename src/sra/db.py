from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from sra.config import settings

# Fail fast when Postgres is down or unreachable: without it a connect can hang
# for the OS TCP timeout, which blocks a whole chat turn.
CONNECT_TIMEOUT_SECONDS = 5


@contextmanager
def connect(*, readonly: bool = False) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """The one place a database connection is opened.

    readonly selects the least-privileged role's DSN. application_name makes
    the two roles distinguishable in pg_stat_activity.
    """
    cfg = settings()
    with psycopg.connect(
        cfg.readonly_dsn if readonly else cfg.dsn,
        row_factory=dict_row,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        application_name="sra-readonly" if readonly else "sra",
    ) as conn:
        yield conn
