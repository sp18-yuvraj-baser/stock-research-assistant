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


@pytest.mark.parametrize(
    "sql",
    [
        # A ';' or quote inside a dollar-quoted string is data, not syntax.
        "SELECT $$a;b$$ AS s",
        "SELECT $$it's; fine$$",
        "SELECT $tag$ x ; $$ y $tag$ AS s",
        "SELECT $q$ one ' two $q$, 'a;b'",
        # $1 is a parameter, not a dollar quote; a $ inside an identifier is too.
        "SELECT $1",
        "SELECT a$b$c FROM t",
        # A backslash-escaped quote inside an E'' string does not end it.
        r"SELECT E'it\'s; fine'",
    ],
)
def test_accepts_dollar_and_escape_strings_hiding_semicolons(sql: str) -> None:
    assert assert_single_read_query(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # A lone quote inside $$..$$ used to make the masker open a literal and
        # blank the real ';' after it.
        "SELECT $$'$$; SELECT pg_sleep(30); SELECT '",
        "SELECT $x$'$x$ ; SELECT 2 ; SELECT 'a'",
        # A dollar-quoted string that is closed early still leaves a real ';'.
        "SELECT $$a$$; DROP TABLE facts",
        # E'' strings: \' does not close the literal, '' afterwards does.
        r"SELECT E'\'' ; SELECT 2 ; SELECT 'a'",
        # Unterminated dollar quote: everything after it is masked, and the
        # leading statement still ends in a visible ';'.
        "SELECT 1; SELECT $$",
    ],
)
def test_rejects_multi_statement_hidden_by_quote_confusion(sql: str) -> None:
    with pytest.raises(UnsafeSqlError):
        assert_single_read_query(sql)
