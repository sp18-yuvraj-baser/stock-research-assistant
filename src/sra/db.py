from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from sra.config import settings


@contextmanager
def connect(*, readonly: bool = False) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    dsn = settings().readonly_dsn if readonly else settings().dsn
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        yield conn
