# Stock Research Assistant

Question answering over SEC filings. Every figure is traceable to an accession
number; nothing is generated.

Research tool, not investment advice. It surfaces and cites what filings say and
declines to recommend.

## Status

Weeks 1-2 of 6 complete: the structured (XBRL) path, the narrative (RAG) path,
`run_sql`, `search_filings`, and the agent loop. The explicit router, eval
harness, restatement handling, and UI are not built yet.

## The rule everything follows from

Numbers never come from retrieval. Exact figures live in `facts` and are reached
with SQL. Narrative and reasoning will come from RAG over filing text. The
`accession_no` foreign key on both `facts` and `chunks` is what lets a hybrid
answer cite a figure and the text discussing it to the same filing.

## Setup

Requires PostgreSQL with pgvector, and ollama with a tool-capable model.

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"
cp .env.example .env   # set SRA_SEC_USER_AGENT to include a real contact email
sra migrate
sra ingest             # XBRL facts: NVDA AAPL MSFT COST WMT, ~20s
sra index              # filing text: parse, chunk, embed, ~3min
```

`sra index` needs an embedding model whose dimensions match the `chunks` table:

```bash
ollama pull nomic-embed-text
```

`SRA_SEC_USER_AGENT` must contain an email address. SEC returns HTTP 403
otherwise.

## Use

```bash
sra ask "What was Nvidia's revenue in FY2025?"
sra ask --show-sql "What was Nvidia's revenue last quarter?"
sra ask "What risks does Nvidia cite around export controls?"
```

## Splitting filings at Item boundaries

Item boundaries are meaningful, so they are found first and chunking happens
inside them. A fixed window would merge the end of Risk Factors into the start
of MD&A and make the citation wrong.

Finding those boundaries is harder than it looks, and every rule below exists
because a real filer broke the previous one:

- `Item 1A. Risk Factors` appears a dozen times mid-sentence as a
  cross-reference. A heading is therefore only recognised when it occupies a
  whole block on its own.
- Styling cannot be used to identify headings: fonts, weights and casing differ
  for every filer.
- Tables cannot simply be stripped. The table of contents lives in one, but
  Walmart sets its real Item headings as one-row tables. Tables are classified
  by shape instead, and only data tables are dropped.
- Microsoft prints a bare `Item 1A` running header above
  `ITEM 1A. RISK FACTORS`, so repeated headings are collapsed to the titled one.
- A 10-Q restarts Item numbering in each Part: MD&A is Part I Item 2, while Risk
  Factors is Part II Item 1A. Sections are keyed on both.
- Filings list their Items twice, in the contents and as real headings, and both
  sequences ascend. The run covering the most text is taken as the body.

Item titles are then normalised to their statutory wording, so a citation reads
the same whichever filer it came from.

## Two traps the schema exists to avoid

**Fiscal labels are derived, never copied.** The SEC company-facts API reports
`fy`/`fp` at the *filing* level: every fact in Nvidia's FY2026 10-K carries
`fy=2026`, including the FY2025 and FY2024 comparatives. Copying that onto the
fact is the easiest way to answer "revenue in FY2025" with the wrong year's
number. Instead each fact is labelled by matching its `period_end` against the
`period_of_report` of the 10-K/10-Q that was actually about that period. No
assumption is made about December year ends or calendar quarters. Facts with no
anchor keep a NULL label rather than a guessed one.

**`period_type` separates a quarter from a year-to-date total.** A 10-Q tags
both with the same `period_end` and the same fiscal label. For Nvidia's Q2
FY2027 those are 96.221B and 177.837B. Queries must filter on `period_type`.

## Layout

| Path | What |
|---|---|
| `sql/` | schema and the read-only role's grants |
| `src/sra/sec/` | rate-limited, disk-cached SEC client |
| `src/sra/ingest/` | idempotent companies / filings / facts ingest, fiscal labelling |
| `src/sra/tools/` | `run_sql` and its guard |
| `src/sra/agent/` | system prompt and the tool loop |

## run_sql safety

Three independent layers: the `sra_reader` role holds no write grants and
defaults to read-only transactions, the connection opens `READ ONLY`, and only a
single `SELECT`/`WITH` statement is accepted. A 5s `statement_timeout` is set
per transaction.

## Checks

```bash
ruff format . && ruff check . && mypy && pytest
```
