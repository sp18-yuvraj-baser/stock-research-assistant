import argparse
import sys

import psycopg
from pydantic import ValidationError

from sra.agent.compose import ask
from sra.agent.loop import OllamaError
from sra.config import configure_logging, settings
from sra.feeds import FeedError
from sra.ingest.announcements_run import ingest_announcements
from sra.ingest.fundamentals_run import FundamentalsIngestReport, ingest_fundamentals
from sra.ingest.funds_run import ingest_funds
from sra.ingest.instruments_run import ingest_all_indian_equities, ingest_instruments
from sra.migrate import apply_migrations
from sra.upstox.auth import authorize_url, exchange_code
from sra.upstox.client import UpstoxError
from sra.upstox.instruments import UnknownInstrumentError


class UsageError(Exception):
    """A bad or missing argument, reported as one line without a traceback."""


def _cmd_migrate(_args: argparse.Namespace) -> int:
    try:
        applied = apply_migrations()
    except ValueError as exc:
        # Raised for unusable DSNs (no database name, reader DSN without a
        # password): configuration the user can fix.
        raise UsageError(str(exc)) from exc
    for name in applied:
        print(f"applied {name}")
    if not applied:
        print("up to date")
    return 0


def _cmd_upstox_login(_args: argparse.Namespace) -> int:
    cfg = settings()
    url = authorize_url(
        api_key=cfg.upstox_api_key, redirect_uri=cfg.upstox_redirect_uri
    )
    print(f"Open this URL, log in, and approve access:\n\n  {url}\n")
    print("Your browser will redirect with a `code` query parameter. Paste it here:")
    token = exchange_code(input("code: ").strip())
    print(
        f"Saved. Obtained at {token.obtained_at.isoformat()}; "
        "re-run `sra upstox login` daily (Upstox tokens expire each day)."
    )
    return 0


def _cmd_upstox_ingest_instruments(args: argparse.Namespace) -> int:
    if args.all and args.tickers:
        raise UsageError("give either tickers or --all, not both")
    if not args.all and not args.tickers:
        raise UsageError("give one or more tickers, or --all")
    result = (
        ingest_all_indian_equities(refresh=args.refresh)
        if args.all
        else ingest_instruments(args.tickers, refresh=args.refresh)
    )
    if args.all:
        print(f"synced {len(result.synced)} NSE/BSE cash-equity listings")
    else:
        for equity in result.synced:
            print(f"{equity.ticker:<10}{equity.exchange:<10}{equity.instrument_key}")
    if result.skipped:
        names = ", ".join(f"{s.ticker} ({s.exchange})" for s in result.skipped)
        print(
            f"skipped {len(result.skipped)} listing(s) whose ticker is already "
            f"stored under a different instrument key: {names}",
            file=sys.stderr,
        )
    return 0


def _cmd_ingest_announcements(_args: argparse.Namespace) -> int:
    report = ingest_announcements()
    print(
        f"{report.seen} announcements in the feed, {report.stored} new, "
        f"{report.unmapped} without a matching ISIN"
    )
    return 0


def _cmd_ingest_funds(_args: argparse.Namespace) -> int:
    print(f"stored {ingest_funds()} mutual-fund schemes")
    return 0


def _cmd_upstox_ingest_fundamentals(args: argparse.Namespace) -> int:
    if args.all and args.tickers:
        raise UsageError("give either tickers or --all, not both")
    if not args.all and not args.tickers:
        raise UsageError("give one or more tickers, or --all")

    def show_progress(current: int, total: int, item: FundamentalsIngestReport) -> None:
        # The report is intentionally formatted via its known fields here so
        # long full-universe jobs remain observable without a noisy line for
        # every successfully resumed listing.
        if item.error or current == total or current % 25 == 0:
            status = "failed" if item.error else "saved"
            print(f"[{current}/{total}] {item.ticker}: {status}", file=sys.stderr)

    reports = ingest_fundamentals(
        args.tickers,
        all_equities=args.all,
        resume=args.all,
        on_progress=show_progress if args.all else None,
    )
    saved = sum(not r.skipped and not r.error for r in reports)
    skipped = sum(r.skipped for r in reports)
    failed = [r for r in reports if r.error]
    print(
        f"fundamentals: {saved} saved, {skipped} already current, {len(failed)} failed"
    )
    for report in failed:
        print(f"{report.ticker}: {report.error}", file=sys.stderr)
    return 1 if failed else 0


def _cmd_ask(args: argparse.Namespace) -> int:
    answer = ask(" ".join(args.question))
    if args.explain:
        print(f"route: {answer.route.value}", file=sys.stderr)
        for reason in answer.routing.reasons:
            print(f"  because {reason}", file=sys.stderr)
    if args.show_sql:
        for path in answer.paths:
            for i, call in enumerate(path.sql_calls, start=1):
                print(f"--- query {i} ---\n{call.sql}\n{call.to_text()}\n")
            for i, quote in enumerate(path.quote_calls, start=1):
                print(f"--- quote {i} ---\n{quote.to_text()}\n")
    print(answer.text)
    queries = sum(len(p.sql_calls) for p in answer.paths)
    quotes = sum(len(p.quote_calls) for p in answer.paths)
    print(
        f"\n[route={answer.route.value}, {answer.rounds} round(s), "
        f"{queries} query(ies), {quotes} quote(s)]",
        file=sys.stderr,
    )
    return 1 if answer.stopped_early else 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Run the local-only Indian equity research dashboard."""
    from sra.dashboard.app import run

    run(host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sra")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate", help="create the database and apply sql/")
    migrate.set_defaults(func=_cmd_migrate)

    ask_cmd = sub.add_parser("ask", help="ask about Indian equities or finance")
    ask_cmd.add_argument("question", nargs="+")
    ask_cmd.add_argument(
        "--explain",
        action="store_true",
        help="print the route taken and the features that decided it",
    )
    ask_cmd.add_argument(
        "--show-sql",
        action="store_true",
        help="print every query the agent ran and the rows it got back",
    )
    ask_cmd.set_defaults(func=_cmd_ask)

    dashboard = sub.add_parser(
        "dashboard", help="run the local Indian-equity research dashboard"
    )
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8000)
    dashboard.set_defaults(func=_cmd_dashboard)

    upstox = sub.add_parser("upstox", help="Upstox (NSE/BSE live quotes) integration")
    upstox_sub = upstox.add_subparsers(dest="upstox_command", required=True)

    upstox_login = upstox_sub.add_parser(
        "login", help="authorize and cache a daily Upstox access token"
    )
    upstox_login.set_defaults(func=_cmd_upstox_login)

    upstox_ingest = upstox_sub.add_parser(
        "ingest-instruments", help="sync NSE/BSE equity listings from Upstox"
    )
    upstox_ingest.add_argument(
        "tickers", nargs="*", help="specific NSE/BSE symbols to sync"
    )
    upstox_ingest.add_argument(
        "--all", action="store_true", help="sync every NSE/BSE cash-equity listing"
    )
    upstox_ingest.add_argument(
        "--refresh", action="store_true", help="bypass the instrument-master cache"
    )
    upstox_ingest.set_defaults(func=_cmd_upstox_ingest_instruments)

    upstox_fund = upstox_sub.add_parser(
        "ingest-fundamentals",
        help="ingest P/E, ROE, revenue etc. for synced Indian listings",
    )
    upstox_fund.add_argument(
        "tickers",
        nargs="*",
        help="specific NSE/BSE symbols to research",
    )
    upstox_fund.add_argument(
        "--all",
        action="store_true",
        help="refresh fundamentals for every synced listing; this can take a long time",
    )
    upstox_fund.set_defaults(func=_cmd_upstox_ingest_fundamentals, resumable=True)

    ingest = sub.add_parser("ingest", help="store free daily feeds (run from cron)")
    ingest_sub = ingest.add_subparsers(dest="ingest_command", required=True)
    ingest_sub.add_parser(
        "announcements",
        help="store BSE's latest corporate announcements (covers about a day)",
    ).set_defaults(func=_cmd_ingest_announcements)
    ingest_sub.add_parser(
        "funds", help="store every mutual fund's latest NAV from AMFI (daily)"
    ).set_defaults(func=_cmd_ingest_funds)

    return parser


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        configure_logging()
        return int(args.func(args))
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (
        OllamaError,
        UpstoxError,
        UnknownInstrumentError,
        FeedError,
    ) as exc:
        # An unreachable local model is the most common setup failure; a
        # traceback tells the user nothing they can act on.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except psycopg.OperationalError as exc:
        print(
            f"error: cannot use the database (is Postgres running and "
            f"SRA_DSN correct?): {_one_line(exc)}",
            file=sys.stderr,
        )
        return 2
    except psycopg.Error as exc:
        print(f"error: database error: {_one_line(exc)}", file=sys.stderr)
        return 2
    except ValidationError as exc:
        # Settings could not load; name the fields, never echo their values.
        fields = ", ".join(sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]}))
        print(
            f"error: missing or invalid setting(s): {fields}. "
            "Set them in .env (see .env.example).",
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        message = "cancelled"
        if getattr(args, "resumable", False):
            message += (
                "; completed listings were saved. Re-run the same command to resume."
            )
        print(message, file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
