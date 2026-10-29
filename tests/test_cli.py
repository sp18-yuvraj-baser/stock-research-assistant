from typing import Any

import psycopg
import pytest
from pydantic import ValidationError

from sra import cli
from sra.agent.loop import OllamaError
from sra.config import Settings
from sra.upstox.auth import MissingUpstoxTokenError
from sra.upstox.client import UpstoxAuthError


def _raiser(exc: BaseException) -> Any:
    def fail(*_args: object, **_kwargs: object) -> Any:
        raise exc

    return fail


def _assert_one_line_error(capsys: pytest.CaptureFixture[str], code: int) -> str:
    assert code != 0
    err = capsys.readouterr().err.strip()
    assert err.startswith("error:")
    assert len(err.splitlines()) == 1
    assert "Traceback" not in err
    return err


def test_ingest_instruments_without_tickers_or_all(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "ingest_instruments", _raiser(AssertionError("called")))
    code = cli.main(["upstox", "ingest-instruments"])
    assert "--all" in _assert_one_line_error(capsys, code)


def test_ingest_instruments_rejects_all_with_tickers(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "ingest_all_indian_equities", _raiser(AssertionError("called"))
    )
    code = cli.main(["upstox", "ingest-instruments", "--all", "TCS"])
    _assert_one_line_error(capsys, code)


def test_ingest_fundamentals_argument_errors(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "ingest_fundamentals", _raiser(AssertionError("called")))
    _assert_one_line_error(capsys, cli.main(["upstox", "ingest-fundamentals"]))
    _assert_one_line_error(
        capsys, cli.main(["upstox", "ingest-fundamentals", "--all", "TCS"])
    )


@pytest.mark.parametrize(
    "exc",
    [
        MissingUpstoxTokenError("no Upstox token; run `sra upstox login`"),
        UpstoxAuthError("expired"),
        OllamaError("cannot reach ollama"),
        psycopg.OperationalError("connection refused\n\tIs the server running?"),
    ],
)
def test_expected_failures_are_one_line_errors(
    exc: BaseException,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "ingest_fundamentals", _raiser(exc))
    code = cli.main(["upstox", "ingest-fundamentals", "TCS"])
    err = _assert_one_line_error(capsys, code)
    assert code == 2
    assert "connection refused" in err or str(exc) in err


def test_ask_reports_unreachable_database(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "ask", _raiser(psycopg.OperationalError("down")))
    _assert_one_line_error(capsys, cli.main(["ask", "hello"]))


def test_missing_settings_name_fields_but_not_values(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        Settings(_env_file=None)  # type: ignore[call-arg]
    except ValidationError as exc:
        error = exc
    else:  # pragma: no cover - the test env sets no SRA_DSN
        pytest.skip("settings are fully configured in this environment")
    monkeypatch.setattr(cli, "ask", _raiser(error))
    code = cli.main(["ask", "hello"])
    err = _assert_one_line_error(capsys, code)
    assert "dsn" in err


def test_migrate_value_error_is_a_user_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "apply_migrations",
        _raiser(ValueError("SRA_READONLY_DSN must carry both a user and a password")),
    )
    _assert_one_line_error(capsys, cli.main(["migrate"]))


def test_migrate_reports_applied_files(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "apply_migrations", lambda: ["001_a.sql"])
    assert cli.main(["migrate"]) == 0
    assert "applied 001_a.sql" in capsys.readouterr().out


def test_interrupt_message_only_mentions_resume_for_ingest(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "ask", _raiser(KeyboardInterrupt()))
    assert cli.main(["ask", "hello"]) == 130
    assert "resume" not in capsys.readouterr().err

    monkeypatch.setattr(cli, "ingest_fundamentals", _raiser(KeyboardInterrupt()))
    assert cli.main(["upstox", "ingest-fundamentals", "--all"]) == 130
    assert "resume" in capsys.readouterr().err


def test_fundamentals_summary_for_explicit_tickers(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sra.ingest.fundamentals_run import FundamentalsIngestReport

    monkeypatch.setattr(
        cli,
        "ingest_fundamentals",
        lambda *_a, **_k: [FundamentalsIngestReport("TCS", 6, 26)],
    )
    assert cli.main(["upstox", "ingest-fundamentals", "TCS"]) == 0
    assert "1 saved" in capsys.readouterr().out


def test_other_database_errors_are_one_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    exc = psycopg.errors.UndefinedTable('relation "instruments" does not exist\nLINE 1')
    monkeypatch.setattr(cli, "ask", _raiser(exc))
    code = cli.main(["ask", "hello"])
    err = _assert_one_line_error(capsys, code)
    assert "instruments" in err


def test_fundamentals_failure_exits_nonzero_and_keeps_the_summary(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sra.ingest.fundamentals_run import FundamentalsIngestReport

    monkeypatch.setattr(
        cli,
        "ingest_fundamentals",
        lambda *_a, **_k: [
            FundamentalsIngestReport("TCS", 6, 26),
            FundamentalsIngestReport("BAD", 0, 0, error="boom"),
        ],
    )
    assert cli.main(["upstox", "ingest-fundamentals", "TCS", "BAD"]) == 1
    captured = capsys.readouterr()
    assert "1 saved" in captured.out and "1 failed" in captured.out
    assert "BAD: boom" in captured.err


def test_ingest_instruments_all_reports_skipped_conflicts(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from sra.ingest.instruments_run import InstrumentSyncResult, SkippedInstrument
    from sra.upstox.instruments import EquityInstrument

    synced = [
        EquityInstrument(f"T{i}", f"NSE_EQ|{i}", "NSE_EQ", "X") for i in range(10)
    ]
    monkeypatch.setattr(
        cli,
        "ingest_all_indian_equities",
        lambda **_k: InstrumentSyncResult(
            synced, [SkippedInstrument("TMPV", "NSE_EQ", "NSE_EQ|INE155A01022")]
        ),
    )
    assert cli.main(["upstox", "ingest-instruments", "--all"]) == 0
    captured = capsys.readouterr()
    assert "synced 10" in captured.out
    assert "skipped 1" in captured.err and "TMPV" in captured.err
