-- Structured + narrative store for SEC filings.
-- Both facts and chunks key on accession_no, which is what makes a hybrid
-- answer citable: a figure and the text discussing it resolve to one filing.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS companies (
  cik              CHAR(10) PRIMARY KEY,
  ticker           TEXT,
  name             TEXT NOT NULL,
  fiscal_year_end  TEXT              -- 'MMDD' as reported by EDGAR; never assume December
);

CREATE TABLE IF NOT EXISTS filings (
  accession_no     TEXT PRIMARY KEY,
  cik              CHAR(10) NOT NULL REFERENCES companies(cik),
  form_type        TEXT NOT NULL,    -- 10-K, 10-Q, 8-K, 10-K/A
  filed_date       DATE,
  period_of_report DATE,
  is_amendment     BOOLEAN NOT NULL DEFAULT FALSE,
  amends           TEXT REFERENCES filings(accession_no)
);

CREATE INDEX IF NOT EXISTS filings_cik_form_period_idx
  ON filings (cik, form_type, period_of_report DESC);

-- period_type distinguishes figures a 10-Q tags with the same period_end:
-- the discrete quarter and the cumulative year-to-date total.
CREATE TABLE IF NOT EXISTS facts (
  id            BIGSERIAL PRIMARY KEY,
  cik           CHAR(10) NOT NULL REFERENCES companies(cik),
  accession_no  TEXT NOT NULL REFERENCES filings(accession_no),
  taxonomy      TEXT NOT NULL,       -- us-gaap, dei, ifrs-full
  tag           TEXT NOT NULL,       -- Revenues, GrossProfit
  unit          TEXT NOT NULL,       -- USD, shares, USD/shares
  value         NUMERIC NOT NULL,
  period_start  DATE,                -- NULL for instantaneous facts
  period_end    DATE NOT NULL,
  -- Stored surrogate so the natural key can be a plain column list: an
  -- expression index on COALESCE() cannot be inferred by ON CONFLICT.
  period_start_key DATE NOT NULL
    GENERATED ALWAYS AS (COALESCE(period_start, DATE '0001-01-01')) STORED,
  duration_days INT,                 -- NULL for instantaneous facts
  period_type   TEXT NOT NULL,       -- instant | quarter | ytd | annual | other
  fiscal_year   INT,                 -- the FILER's label for this fact's period; derived, not copied
  fiscal_period TEXT,                -- FY, Q1..Q4
  form          TEXT NOT NULL,       -- form of the filing this value was reported in
  filed_fy      INT,                 -- raw companyfacts "fy": the FILING's fiscal year, not the fact's
  filed_fp      TEXT,                -- raw companyfacts "fp"
  superseded    BOOLEAN NOT NULL DEFAULT FALSE,
  CONSTRAINT facts_period_type_chk
    CHECK (period_type IN ('instant', 'quarter', 'ytd', 'annual', 'other'))
);

-- One row per (fact, reporting filing). Makes ingest idempotent and lets a
-- restatement coexist with the original figure rather than overwriting it.
CREATE UNIQUE INDEX IF NOT EXISTS facts_natural_key_uidx
  ON facts (cik, taxonomy, tag, unit, period_end, period_start_key, accession_no);

CREATE INDEX IF NOT EXISTS facts_lookup_idx
  ON facts (cik, tag, fiscal_year, fiscal_period, period_type);
CREATE INDEX IF NOT EXISTS facts_period_idx
  ON facts (cik, tag, period_end DESC);

CREATE TABLE IF NOT EXISTS chunks (
  id            BIGSERIAL PRIMARY KEY,
  accession_no  TEXT NOT NULL REFERENCES filings(accession_no),
  section       TEXT,                -- 'Item 7 MD&A', 'Item 1A Risk Factors'
  ordinal       INT NOT NULL,
  text          TEXT NOT NULL,
  embedding     VECTOR(768)
);

CREATE UNIQUE INDEX IF NOT EXISTS chunks_natural_key_uidx
  ON chunks (accession_no, section, ordinal);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
  ON chunks USING hnsw (embedding vector_cosine_ops);

-- Latest-filed value for each distinct period. A restatement supersedes the
-- original. Week 5 replaces this with an explicit amendment rule.
CREATE OR REPLACE VIEW facts_current AS
SELECT DISTINCT ON (f.cik, f.taxonomy, f.tag, f.unit, f.period_start, f.period_end)
       f.*, fl.filed_date, fl.period_of_report
FROM facts f
JOIN filings fl ON fl.accession_no = f.accession_no
WHERE NOT f.superseded
ORDER BY f.cik, f.taxonomy, f.tag, f.unit, f.period_start, f.period_end,
         fl.filed_date DESC, f.accession_no DESC;
