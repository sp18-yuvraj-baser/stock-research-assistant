# Indian Stock Research Assistant

Local research chat for NSE and BSE cash equities. It provides evidence-backed
facts, live quotes, and finance education. It does not give personalised buy,
sell, or hold advice, and no return is guaranteed.

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

## Use

Run the local chat:

```bash
sra dashboard --port 8001
```

Open <http://127.0.0.1:8001>. Ask about an NSE/BSE company, live prices,
fundamentals, a holding you want reviewed, or general finance concepts.
Follow-up questions keep the context of the chat; the browser holds the
conversation and "New chat" clears it.

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
