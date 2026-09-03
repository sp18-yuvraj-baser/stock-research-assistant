COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"


def normalize_cik(cik: str | int) -> str:
    """EDGAR paths use a bare CIK; the JSON APIs use a zero-padded 10-digit one."""
    return str(int(cik)).zfill(10)


def submissions(cik: str | int) -> str:
    return f"https://data.sec.gov/submissions/CIK{normalize_cik(cik)}.json"


def submissions_page(file_name: str) -> str:
    return f"https://data.sec.gov/submissions/{file_name}"


def company_facts(cik: str | int) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{normalize_cik(cik)}.json"


def filing_index(cik: str | int, accession_no: str) -> str:
    bare_accession = accession_no.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}"
        f"/{bare_accession}/index.json"
    )


def filing_document(cik: str | int, accession_no: str, document: str) -> str:
    bare_accession = accession_no.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}"
        f"/{bare_accession}/{document}"
    )
