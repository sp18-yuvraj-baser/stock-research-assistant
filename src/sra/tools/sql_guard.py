class UnsafeSqlError(ValueError):
    pass


_ALLOWED_PREFIXES = ("select", "with")


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
        elif char in ("'", '"'):
            quote = char
            out.append(" ")
            i += 1
            while i < n:
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
