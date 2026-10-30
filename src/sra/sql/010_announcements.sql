-- BSE corporate announcements, stored by `sra ingest announcements`. BSE's
-- public feed only covers about the last day, so history builds up from the
-- first run; run it daily (cron) to keep it complete.

CREATE TABLE IF NOT EXISTS announcements (
  id           BIGSERIAL PRIMARY KEY,
  bse_code     TEXT NOT NULL,
  isin         TEXT,               -- from Upstox's instrument file; NULL if unknown
  company      TEXT NOT NULL,      -- as BSE names it
  description  TEXT NOT NULL,
  category     TEXT NOT NULL,      -- results, board_meeting, insider, transcript,
                                   -- annual_report, presentation, shareholding,
                                   -- corporate_action, other
  pdf_url      TEXT NOT NULL UNIQUE,
  published_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS announcements_isin_published_idx
  ON announcements (isin, published_at DESC);
CREATE INDEX IF NOT EXISTS announcements_category_published_idx
  ON announcements (category, published_at DESC);

GRANT SELECT ON announcements TO sra_reader;
