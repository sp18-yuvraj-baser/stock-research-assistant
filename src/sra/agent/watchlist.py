"""Keep a watchlist from the chat and show it with live prices.

"Add TCS and Infosys to my watchlist", "remove ITC from my watchlist",
"show my watchlist". Showing it is one batched quote plus each stock's
52-week range; nothing here is a buy or sell signal.
"""

import re
from datetime import date

import psycopg

from sra.agent.compare import companies_in
from sra.agent.history import price_stats
from sra.agent.loop import PathResult
from sra.db import connect
from sra.figures import rupees
from sra.upstox.client import UpstoxError
from sra.upstox.market import daily_candles, full_quotes

MAX_RANGES = 15  # 52-week ranges take a call each; keep a long list fast

NO_TABLE = "The watchlist is not set up yet. Run `sra migrate` first."
EMPTY = 'Your watchlist is empty. Add stocks with "add TCS to my watchlist".'


def _edit(question: str, *, add: bool) -> PathResult:
    named = companies_in(question)
    if not named:
        return PathResult(
            text='Name the stocks, e.g. "add TCS and Infosys to my watchlist".'
        )
    with connect() as conn:
        for company in named:
            if add:
                conn.execute(
                    "INSERT INTO watchlist (instrument_key, ticker) VALUES (%s, %s) "
                    "ON CONFLICT (instrument_key) DO NOTHING",
                    (company.instrument_key, company.ticker),
                )
            else:
                conn.execute(
                    "DELETE FROM watchlist WHERE instrument_key = %s",
                    (company.instrument_key,),
                )
        conn.commit()
    done = f"{'Added' if add else 'Removed'}: {', '.join(c.ticker for c in named)}"
    return PathResult(text=f"Watchlist updated\n{done}\n\n{show().text}")


def show(*, today: date | None = None) -> PathResult:
    today = today or date.today()
    with connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT instrument_key, ticker FROM watchlist ORDER BY added_at, ticker"
        ).fetchall()
    if not rows:
        return PathResult(text=EMPTY)
    keys = [str(r["instrument_key"]) for r in rows]
    try:
        quotes = full_quotes(keys)
    except UpstoxError as exc:
        return PathResult(text=f"Live prices are unavailable right now ({exc}).")
    lines = []
    for position, row in enumerate(rows):
        key, ticker = str(row["instrument_key"]), str(row["ticker"])
        quote = quotes.get(key)
        if quote is None:
            lines.append(f"  {ticker}: no live price")
            continue
        line = f"  {ticker}: {rupees(quote.last_price)}"
        if quote.change_pct is not None:
            line += f" ({quote.change_pct:+.2f}% today)"
        if position < MAX_RANGES:
            try:
                stats = price_stats(daily_candles(key, years=1, today=today),
                                    quote.last_price, today)  # fmt: skip
            except UpstoxError:
                stats = None
            if stats is not None:
                below = 100 * (1 - quote.last_price / stats.high.price)
                line += f", {below:.1f}% below its 52-week high"
        lines.append(line)
    return PathResult(
        text="\n\n".join(
            [
                f"Your watchlist\n{len(rows)} stocks at live prices.",
                "Stocks\n" + "\n".join(lines),
                'Ask "Should I buy <SYMBOL>?" for a full review of any of them.',
            ]
        )
    )


def answer(question: str) -> PathResult:
    text = question.lower()
    try:
        if re.search(r"\b(add|put|include|track)\b", text):
            return _edit(question, add=True)
        if re.search(r"\b(remove|delete|drop|take off|untrack)\b", text):
            return _edit(question, add=False)
        return show()
    except psycopg.errors.UndefinedTable:
        return PathResult(text=NO_TABLE)
