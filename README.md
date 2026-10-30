# Indian Stock Research Assistant

Local research chat for NSE and BSE cash equities. It provides evidence-backed
facts, live quotes, finance education, and a scored buy/hold/sell call on a
stock you own or are considering. The call comes from a fixed scoring of the
data, not from knowing your finances; it is not personalised advice, and no
return is guaranteed.

## Setup

Requires Python 3.12+, PostgreSQL and [Ollama](https://ollama.com). Copy
`.env.example` to `.env` and fill in the database DSNs and Upstox credentials
(an Analytics Token is enough; see the comments in that file). Pull the model
named by `SRA_GENERATION_MODEL`, then install and migrate:

```bash
ollama pull gemma4:latest
uv venv --python 3.13
source .venv/bin/activate
uv pip install -e ".[dev]"
sra migrate
```

`sra migrate` creates the database if needed and applies the files in `src/sra/sql/`.
`SRA_READONLY_DSN` must use the role name `sra_reader`: the SQL files grant to
that role by name, and `sra migrate` refuses any other user.

**After upgrading, run `sra migrate`.** Migration 007 permanently drops the
old SEC tables and their data, and migration 008 adds
`fundamental_financials.period_end`, which the fundamentals prompt needs.

If you use OAuth app credentials instead of an Analytics Token, run
`sra upstox login` to cache the daily access token.

## Build NSE/BSE coverage

Sync the complete Upstox cash-equity master before asking about arbitrary NSE
or BSE symbols (or pass specific symbols instead of `--all`; `--refresh`
bypasses the cached instrument master):

```bash
sra upstox ingest-instruments --all
```

Fundamentals are never refreshed automatically: they change only when you run
`sra upstox ingest-fundamentals`, with explicit tickers or `--all` for every
synced listing. `--all` is resumable and skips listings whose ratios were
stored within the last hour (explicit tickers are always refetched). A full
run can take a substantial time because the provider is queried for each
company. The command exits non-zero if any listing failed.

If the master lists a ticker under an instrument key different from the one
already stored, `ingest-instruments --all` skips that listing and reports it;
stored rows are never rekeyed.

```bash
sra upstox ingest-fundamentals --all
```

Upstox may not return every ratio for every company. Missing or unavailable
provider data stays visible as a gap; the assistant does not infer it.

Each fundamentals refresh also stores cash flow, the company's sector label
and its recent corporate actions (dividends), which sector lists, peers and
dividend-yield lists use.

BSE corporate announcements (results, board meetings, insider disclosures,
transcripts, annual reports) come from BSE's public feed, which only covers
the current day. Store them regularly, for example hourly from cron:

```bash
sra ingest announcements
# crontab: 0 * * * * cd /path/to/repo && .venv/bin/sra ingest announcements
```

Mutual-fund NAVs come from AMFI's daily file (about 14,000 schemes); fund
returns are computed from api.mfapi.in's NAV history when asked:

```bash
sra ingest funds   # daily, e.g. from cron
```

Shareholding patterns and mutual-fund portfolio holdings need an optional
free key from [BharatStock](https://bharatstockapi.com) (60 requests a day),
set as `SRA_BHARATSTOCK_API_KEY`; answers are cached in the database to stay
under that limit. IPOs and bulk/block deals are not covered: there is no
free, reliable source for them.

## Buy/sell calls

A question about keeping, selling or buying one stock gets a call: buy more,
hold, reduce or sell if you own it; buy, wait or avoid if you don't. The
call is scored in code from up to four groups of signals, each capped at
±2 so no single group decides it:

| Group | Signal (+1 / -1) |
|---|---|
| Valuation | P/E, P/B, EV/EBITDA cheaper / pricier than sector (a negative P/E is -2) |
| Quality | ROE, ROCE, ROA and bank ratios better / weaker than sector |
| Growth | latest yearly revenue and net profit up / down |
| Trend | price above / below its 200-day average; 1-year move beyond ±15% |

An owner's score of +3 or more is buy more, -1 to +2 hold, -2 or -3 reduce,
-4 or less sell; for someone who doesn't own the stock, +3 or more is buy,
-1 to +2 wait, -2 or less avoid. Fewer than three signals keeps the call at
hold or wait with low conviction. The local model then writes the note in an
analyst's voice; it must state the given call and may only use numbers from
the evidence, or the note is written in code instead. The evidence, the
points for and against, and what would change the call are always shown
under the note.

## Use

Run the local chat:

```bash
sra dashboard --port 8001
```

Open <http://127.0.0.1:8001>. Follow-up questions keep the context of the
chat; the browser holds the conversation and "New chat" clears it. In Chrome,
Edge and Safari a microphone button fills the box from speech.

What it answers (every figure computed in code from real data):

| Ask | Example |
|---|---|
| Live price and today's session | "What's TCS trading at right now?" |
| Price history, 52-week range, chart | "Reliance 5 year chart", "TCS 52 week high" |
| Market overview, top movers, holidays | "How is the market today?", "top gainers" |
| Buy/sell call on a stock or a portfolio | "I have 100 HDFC Bank shares bought at 709, keep?", "Should I buy TCS?", "my portfolio: 100 TCS at 3500, 50 INFY at 1400" |
| Capital-gains tax estimate | "tax if I sell 100 TCS bought at 2000 in March 2024" |
| Fundamentals, comparisons, results, dividends | "Compare TCS vs Infosys", "TCS latest quarterly results", "ITC dividend history" |
| Screens, sectors, peers | "stocks with ROE above 20% and P/E below 25", "IT stocks", "peers of Infosys", "suggest stocks for long term" |
| News and BSE filings | "latest news on TCS", "insider trading disclosures", "results announced today" |
| Earnings calls, annual reports | "summarise TCS's latest earnings call" |
| Calculators and worked examples | "SIP of 5000 a month for 10 years at 12%", "how is TCS's ROE calculated?" |
| Mutual funds and options | "Parag Parikh flexi cap vs Axis large cap fund", "Nifty option chain" |
| Watchlist | "add TCS and Infosys to my watchlist", "show my watchlist" |
| Concepts | "What is ROE?" |

The server is meant for local use and binds to `127.0.0.1` by default. It has
no authentication, so do not expose it with `--host 0.0.0.0` on a shared
network. It answers only requests whose `Host` is `localhost`, `127.0.0.1`
or `[::1]` (any port), plus the concrete host given to `--host`; this blocks
DNS-rebinding attacks. Binding to `0.0.0.0` or `::` does not widen that list,
so reach it by a loopback name. It also rejects cross-origin POST requests and
sends strict security headers. `GET /api/health` reports whether the database and the Ollama model
are reachable (HTTP 200 when both are, 503 otherwise).

The same routes work in the terminal:

```bash
sra ask "What is TCS's P/E ratio?"
sra ask "What's INFY trading at right now?"
sra ask "What is the meaning of ROE?"
sra ask --explain "I have 100 HDFC Bank shares, should I keep them?"
sra ask --show-sql "Compare TCS and Infosys ROE"
```

`--explain` prints the route taken and what decided it; `--show-sql` prints
every query run and the rows returned.

## Development

```bash
pytest -q
ruff check . && ruff format --check .
mypy src
```
