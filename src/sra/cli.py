import argparse
import sys

from sra.agent.loop import ask
from sra.ingest.narrative_run import index_narrative
from sra.ingest.run import DEFAULT_TICKERS, ingest_tickers
from sra.migrate import apply_migrations


def _cmd_migrate(_args: argparse.Namespace) -> int:
    for name in apply_migrations():
        print(f"applied {name}")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    reports = ingest_tickers(args.tickers or None, refresh=args.refresh)
    print(
        f"{'ticker':<8}{'cik':<12}{'filings':>9}{'facts':>10}"
        f"{'skipped':>9}{'stubs':>8}{'labelled':>10}"
    )
    for r in reports:
        print(
            f"{r.ticker:<8}{r.cik:<12}{r.filings:>9}{r.facts_staged:>10}"
            f"{r.facts_skipped:>9}{r.stub_filings:>8}{r.fiscal_labels:>10}"
        )
    return 0


def _cmd_index(args: argparse.Namespace) -> int:
    reports = index_narrative(
        args.tickers or None, annual=args.annual, quarterly=args.quarterly
    )
    print(f"{'ticker':<8}{'form':<7}{'accession':<24}{'sections':>9}{'chunks':>8}")
    for r in reports:
        print(
            f"{r.ticker:<8}{r.form_type:<7}{r.accession_no:<24}"
            f"{r.sections:>9}{r.chunks:>8}"
        )
    print(f"\ntotal chunks: {sum(r.chunks for r in reports)}")
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    answer = ask(" ".join(args.question))
    if args.show_sql:
        for i, call in enumerate(answer.sql_calls, start=1):
            print(f"--- query {i} ---\n{call.sql}\n{call.to_text()}\n")
    print(answer.text)
    print(
        f"\n[{answer.rounds} tool round(s), {len(answer.sql_calls)} query(ies)]",
        file=sys.stderr,
    )
    return 1 if answer.stopped_early else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sra")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate", help="create the database and apply sql/")
    migrate.set_defaults(func=_cmd_migrate)

    ingest = sub.add_parser("ingest", help="ingest XBRL facts for tickers")
    ingest.add_argument(
        "tickers",
        nargs="*",
        help=f"tickers to ingest (default: {' '.join(DEFAULT_TICKERS)})",
    )
    ingest.add_argument(
        "--refresh",
        action="store_true",
        help="bypass the SEC response cache",
    )
    ingest.set_defaults(func=_cmd_ingest)

    index = sub.add_parser("index", help="parse, chunk and embed filing text")
    index.add_argument("tickers", nargs="*")
    index.add_argument("--annual", type=int, default=2, help="10-Ks per company")
    index.add_argument("--quarterly", type=int, default=4, help="10-Qs per company")
    index.set_defaults(func=_cmd_index)

    ask_cmd = sub.add_parser("ask", help="ask a question about the filings")
    ask_cmd.add_argument("question", nargs="+")
    ask_cmd.add_argument(
        "--show-sql",
        action="store_true",
        help="print every query the agent ran and the rows it got back",
    )
    ask_cmd.set_defaults(func=_cmd_ask)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
