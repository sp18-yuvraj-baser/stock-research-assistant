import re


class UnsafeSqlError(ValueError):
    pass


_ALLOWED_PREFIXES = ("select", "with")

# $$ or $tag$ opens a dollar-quoted string. A tag cannot start with a digit, so
# the positional parameter $1 is not one.
_DOLLAR_TAG = re.compile(r"\$(?:[^\W\d]\w*)?\$")


def _is_ident_char(char: str) -> bool:
    return char.isalnum() or char in "_$"


def strip_sql_noise(sql: str) -> str:
    """Blank out string literals and comments, preserving length and newlines.

    Statement-boundary detection has to ignore semicolons that appear inside a
    quoted literal or a comment, so those regions are replaced with spaces
    rather than removed.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        char = sql[i]
        two = sql[i : i + 2]
        if two == "--":
            while i < n and sql[i] != "\n":
                out.append(" ")
                i += 1
        elif two == "/*":
            depth = 1
            out.append("  ")
            i += 2
            while i < n and depth:
                if sql[i : i + 2] == "/*":
                    depth += 1
                    out.append("  ")
                    i += 2
                elif sql[i : i + 2] == "*/":
                    depth -= 1
                    out.append("  ")
                    i += 2
                else:
                    out.append("\n" if sql[i] == "\n" else " ")
                    i += 1
        elif char == "$" and (
            (tag := _DOLLAR_TAG.match(sql, i))
            and not (i and _is_ident_char(sql[i - 1]))
        ):
            # Dollar-quoted text has no escapes: it runs to the next identical
            # tag, and may hold quotes or semicolons that must not be parsed.
            end = sql.find(tag.group(), tag.end())
            stop = n if end == -1 else end + len(tag.group())
            out.extend("\n" if c == "\n" else " " for c in sql[i:stop])
            i = stop
        elif char in ("'", '"'):
            quote = char
            # E'...' strings treat backslash as an escape, so \' does not end
            # them; a plain-literal reading would desync from the server there.
            backslash_escapes = (
                quote == "'"
                and i > 0
                and sql[i - 1] in "eE"
                and not (i > 1 and _is_ident_char(sql[i - 2]))
            )
            out.append(" ")
            i += 1
            while i < n:
                if backslash_escapes and sql[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                if sql[i] == quote:
                    # '' and "" are escaped quotes, not the end of the literal.
                    if sql[i : i + 2] == quote * 2:
                        out.append("  ")
                        i += 2
                        continue
                    out.append(" ")
                    i += 1
                    break
                out.append("\n" if sql[i] == "\n" else " ")
                i += 1
        else:
            out.append(char)
            i += 1
    return "".join(out)


def assert_single_read_query(sql: str) -> str:
    """Reject anything but one SELECT/WITH statement.

    The read-only role already refuses to write, but a second statement could
    still raise the session's statement_timeout and hold the demo machine.
    """
    if not sql or not sql.strip():
        raise UnsafeSqlError("empty query")

    masked = strip_sql_noise(sql)
    body, _, trailing = masked.rstrip().rpartition(";")
    if body and ";" in body:
        raise UnsafeSqlError("only one statement is allowed")
    if body and trailing.strip():
        raise UnsafeSqlError("only one statement is allowed")

    # A parenthesised subquery is a legitimate top-level form: "(SELECT 1)".
    head = masked.strip().lstrip("( \t\r\n")
    first_word = head.split(None, 1)[0].lower() if head else ""
    if first_word not in _ALLOWED_PREFIXES:
        raise UnsafeSqlError(
            f"query must start with SELECT or WITH, got {first_word!r}"
        )
    return sql.strip().rstrip(";").strip()
