import pytest

from sra.tools.sql_guard import UnsafeSqlError, assert_single_read_query


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select value from facts limit 1;",
        "  WITH x AS (SELECT 1) SELECT * FROM x  ",
        "-- a leading comment\nSELECT 1",
        "/* block */ SELECT 1",
        "SELECT 'a; b' AS literal_with_semicolon",
        "SELECT 'it''s fine; really'",
        "SELECT 1 -- trailing; comment",
        "SELECT 1 /* nested /* deeper; */ still */ ",
        "(SELECT 1)",
    ],
)
def test_accepts_single_read_query(sql: str) -> None:
    assert assert_single_read_query(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "INSERT INTO facts VALUES (1)",
        "UPDATE facts SET value = 0",
        "DELETE FROM facts",
        "DROP TABLE facts",
        "SET statement_timeout = '600s'",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE facts",
        "SET statement_timeout='600s'; SELECT 1",
        "SELECT 1;\n-- sneak\nSELECT 2",
        "COPY facts TO '/tmp/x'",
        "EXPLAIN ANALYZE DELETE FROM facts",
    ],
)
def test_rejects_unsafe_query(sql: str) -> None:
    with pytest.raises(UnsafeSqlError):
        assert_single_read_query(sql)


def test_strips_trailing_semicolon() -> None:
    assert assert_single_read_query("SELECT 1;") == "SELECT 1"
