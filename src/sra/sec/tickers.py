from sra.sec import endpoints
from sra.sec.client import SecClient


class UnknownTickerError(LookupError):
    pass


def resolve_tickers(client: SecClient, tickers: list[str]) -> dict[str, str]:
    """Map tickers to zero-padded CIKs. Raises if any ticker is unknown, so a
    typo in an ingest run fails loudly instead of silently ingesting fewer
    companies than asked for."""
    payload = client.get_json(endpoints.COMPANY_TICKERS)
    by_ticker = {
        str(row["ticker"]).upper(): endpoints.normalize_cik(row["cik_str"])
        for row in payload.values()
    }
    resolved: dict[str, str] = {}
    unknown: list[str] = []
    for ticker in tickers:
        key = ticker.upper()
        if key in by_ticker:
            resolved[key] = by_ticker[key]
        else:
            unknown.append(ticker)
    if unknown:
        raise UnknownTickerError(f"no CIK for: {', '.join(unknown)}")
    return resolved
