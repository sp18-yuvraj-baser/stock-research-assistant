"""Option-chain summary, shareholding, and what this assistant cannot cover.

The option chain comes from Upstox for the nearest expiry; put-call ratio,
max pain and the busiest strikes are computed here and explained as market
positioning, never as a trade. Shareholding needs the optional BharatStock
key. IPOs and bulk/block deals have no free data source and say so.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sra import bharatstock
from sra.agent.companies import resolve_companies
from sra.agent.loop import PathResult
from sra.upstox.client import UpstoxError, shared_client
from sra.upstox.market import find_index


@dataclass(frozen=True)
class Strike:
    price: float
    call_oi: float
    put_oi: float
    call_iv: float | None
    put_iv: float | None


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_chain(rows: list[Any]) -> tuple[float | None, list[Strike]]:
    """(underlying spot, strikes) from Upstox's option-chain rows."""
    spot = None
    strikes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        spot = spot or _number(row.get("underlying_spot_price")) or None
        call = row.get("call_options") or {}
        put = row.get("put_options") or {}
        strikes.append(
            Strike(
                _number(row.get("strike_price")),
                _number((call.get("market_data") or {}).get("oi")),
                _number((put.get("market_data") or {}).get("oi")),
                (call.get("option_greeks") or {}).get("iv"),
                (put.get("option_greeks") or {}).get("iv"),
            )
        )
    return spot, sorted(strikes, key=lambda s: s.price)


def max_pain(strikes: list[Strike]) -> float | None:
    """The expiry price at which option buyers would collect the least."""
    if not strikes:
        return None

    def payout(settle: float) -> float:
        return sum(
            s.call_oi * max(0.0, settle - s.price)
            + s.put_oi * max(0.0, s.price - settle)
            for s in strikes
        )

    return min((s.price for s in strikes), key=payout)


def _nearest_expiry(key: str, today: date) -> str | None:
    payload = shared_client().get_json(
        "/v2/option/contract", params={"instrument_key": key}
    )
    expiries = sorted(
        {
            str(c.get("expiry"))
            for c in (payload.get("data") or [])
            if isinstance(c, dict) and c.get("expiry")
        }
    )
    upcoming = [e for e in expiries if e >= today.isoformat()]
    return upcoming[0] if upcoming else None


def options(question: str, *, today: date | None = None) -> PathResult:
    today = today or date.today()
    index = find_index(question)
    if index is not None:
        key, title = index.key, index.name
    else:
        found = resolve_companies(question)
        if len(found.companies) != 1:
            return PathResult(
                text='Name an index or a stock, e.g. "Nifty option chain" or '
                '"TCS options PCR".'
            )
        key, title = found.companies[0].instrument_key, found.companies[0].ticker
    try:
        expiry = _nearest_expiry(key, today)
        if expiry is None:
            return PathResult(text=f"{title}: Upstox lists no upcoming option expiry.")
        payload = shared_client().get_json(
            "/v2/option/chain", params={"instrument_key": key, "expiry_date": expiry}
        )
    except UpstoxError as exc:
        return PathResult(
            text=f"{title}: option data is unavailable right now ({exc})."
        )
    spot, strikes = parse_chain(payload.get("data") or [])
    call_oi = sum(s.call_oi for s in strikes)
    put_oi = sum(s.put_oi for s in strikes)
    if not strikes or call_oi <= 0:
        return PathResult(text=f"{title}: no open interest in the {expiry} chain yet.")
    top_call = max(strikes, key=lambda s: s.call_oi)
    top_put = max(strikes, key=lambda s: s.put_oi)
    lines = [
        f"  Put-call ratio (open interest): {put_oi / call_oi:.2f}",
        f"  Max pain: {max_pain(strikes):,.0f}",
        f"  Most call open interest: {top_call.price:,.0f} strike "
        f"({top_call.call_oi:,.0f})",
        f"  Most put open interest: {top_put.price:,.0f} strike "
        f"({top_put.put_oi:,.0f})",
    ]
    if spot:
        atm = min(strikes, key=lambda s: abs(s.price - spot))
        ivs = ", ".join(
            f"{side} {iv:.1f}%"
            for side, iv in (("call", atm.call_iv), ("put", atm.put_iv))
            if isinstance(iv, int | float)
        )
        lines.insert(0, f"  Underlying: {spot:,.2f}")
        if ivs:
            lines.append(f"  At-the-money ({atm.price:,.0f}) implied volatility: {ivs}")
    return PathResult(
        text="\n\n".join(
            [
                f"{title} options, expiry {expiry}\nUpstox option chain; figures "
                "computed across all listed strikes.",
                "Positioning\n" + "\n".join(lines),
                "How to read it\n"
                "  - Put-call ratio above 1: more puts open than calls.\n"
                "  - Max pain: where the most option premium would expire "
                "worthless; not a price forecast.\n"
                "  - Big open-interest strikes are where many positions sit, not "
                "levels the price must respect.\n"
                "  - Options can lose their whole value; this is not a trade "
                "suggestion.",
            ]
        )
    )


def shareholding(question: str) -> PathResult:
    found = resolve_companies(question)
    if len(found.companies) != 1:
        return PathResult(text='Name one company, e.g. "TCS shareholding pattern".')
    company = found.companies[0]
    title = f"{company.name} ({company.ticker}): shareholding"
    try:
        payload = bharatstock.get(
            f"/stocks/{bharatstock.path_part(company.ticker)}/shareholding",
            {"exchange": "NSE", "page_size": "4"},
            max_age=timedelta(days=7),
        )
    except bharatstock.BharatStockError as exc:
        return PathResult(text=f"{title}\n{exc}")
    quarters = [q for q in (payload.get("data") or []) if isinstance(q, dict)]
    if not quarters:
        return PathResult(text=f"{title}\nBharatStock returned no shareholding data.")
    fields = (
        ("Promoters", "promoter_pct"),
        ("FIIs", "fii_pct"),
        ("DIIs", "dii_pct"),
        ("  of which mutual funds", "mutual_funds_pct"),
        ("Public", "public_pct"),
        ("  of which individuals", "individuals_pct"),
    )
    columns = [str(q.get("as_on_date") or "?") for q in quarters]
    rows = []
    for label, key in fields:
        values = [q.get(key) for q in quarters]
        if all(v is None for v in values):
            continue
        cells = " · ".join(
            f"{when} {v:.2f}%" if isinstance(v, int | float) else f"{when} n/a"
            for when, v in zip(columns, values, strict=True)
        )
        rows.append(f"  {label}: {cells}")
    return PathResult(
        text="\n\n".join(
            [
                f"{title}\nQuarterly filings via BharatStock, latest first.",
                "Holders\n" + "\n".join(rows),
                "Not covered here: pledged promoter shares.",
            ]
        )
    )


NOT_COVERED = {
    "ipo": "IPO details (upcoming issues, subscription, listing gains)",
    "bulk": "bulk and block deals",
}


def not_covered(question: str) -> PathResult:
    lowered = question.lower()
    what = NOT_COVERED["ipo"] if "ipo" in lowered else NOT_COVERED["bulk"]
    return PathResult(
        text=f"Not covered\nThis assistant has no free, reliable source for {what}, "
        "so it does not answer from guesses. The exchanges (nseindia.com, "
        "bseindia.com) publish these directly."
    )
