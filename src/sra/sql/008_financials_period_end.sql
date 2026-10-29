-- Upstox's `period` labels ('Mar 2026') do not sort chronologically as text,
-- so "latest" or "growth vs prior period" cannot be expressed in SQL on them.
-- period_end is the first day of the month the label names, kept as a real
-- date so it orders correctly. NULL when the label is not 'Mon YYYY'.
ALTER TABLE fundamental_financials ADD COLUMN IF NOT EXISTS period_end DATE;

-- to_date() raises on anything it cannot read, so only touch labels that are
-- a real month abbreviation followed by a year.
UPDATE fundamental_financials
SET period_end = to_date(period, 'Mon YYYY')
WHERE period_end IS NULL
  AND period ~ '^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [0-9]{4}$';

-- Serves "latest N periods of a category" lookups. It also covers the prefix
-- the older lookup index served, which the primary key already duplicated.
CREATE INDEX IF NOT EXISTS fundamental_financials_period_end_idx
  ON fundamental_financials
  (instrument_key, statement, time_period, category, period_end DESC);
DROP INDEX IF EXISTS fundamental_financials_lookup_idx;
