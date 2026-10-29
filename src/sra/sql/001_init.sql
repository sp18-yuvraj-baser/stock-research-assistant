-- Superseded: this file used to create the SEC-filings store (companies,
-- filings, facts, chunks, pgvector). That product scope was retired and
-- 007_drop_sec_schema.sql removes those tables. The file is kept as a no-op
-- so migration filenames stay stable for the schema_migrations ledger.
SELECT 1;
