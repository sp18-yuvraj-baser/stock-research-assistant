-- The narrative path needs the filing's main document to fetch its HTML.
-- submissions.json already carries the name, so storing it here avoids an
-- extra request per filing to read the archive index.
ALTER TABLE filings ADD COLUMN IF NOT EXISTS primary_document TEXT;
