"""Market Data MCP server.

Exchange hours, futures expiry, settlement dates, forex sessions, US Treasury yield curve, ECB FX rates and
CFTC COT data as MCP tools. Paid tools cost 0.001-0.002 USDC per call and are paid automatically with x402
(USDC on Base), only to the known API wallets and within MAX_USDC_PER_CALL / MAX_USDC_PER_SESSION.

Environment:
  X402_PRIVATE_KEY      key of a small, dedicated payment wallet (a few USDC only)
  X402_KEY_FILE         alternative: path to a file holding that key (plain text, or JSON with "private_key")
  MAX_USDC_PER_CALL     highest price per call (default 0.01)
  MAX_USDC_PER_SESSION  total budget per server start (default 0.10)
"""
from __future__ import annotations

import os
import threading
from typing import Optional

import requests
from mcp.server.fastmcp import FastMCP

__version__ = "0.2.0"

# ================================================================ Katalog
APIS = {
    "market-hours": "https://market-hours-api.onrender.com",
    "futures-expiry": "https://futures-expiry-api.onrender.com",
    "trading-time": "https://trading-time-api.onrender.com",
    "macro-data": "https://macro-data-api-313m.onrender.com",
    "market-calendar": "https://market-calendar-api.onrender.com",
    "filings-specs": "https://filings-specs-api.onrender.com",
}

# Nur an diese Wallets darf der Server zahlen (Schutz gegen umgeleitete Zahlungen).
ALLOWED_PAY_TO = {
    "0xb2b76adbc021dda03d2b04943079b9d174d5779f",  # Market Hours API
    "0xcb3ae5bf8b1b4b3fe9d987a9a733830095a2927b",  # Futures Expiry API
    "0x98ad309e7ea4ceec93ce61e7d89cf697c2f4fd10",  # Trading Time API
    "0x5d4201f59a0ac8bcdb6106752846a4993395ff31",  # Macro Data API
    "0xc577c27d10767abfc10d867b443577fe296e7d23",  # Market Calendar API
    "0x6bffcc85172372ad4e9c1ffafe97980a2c778fc7",  # Filings & Specs API
}
NETWORK = "eip155:8453"  # Base mainnet
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

# (Tool, API, Pfad, Preis in USDC, Kurzbeschreibung)
TOOLS = [
    ("market_is_open", "market-hours", "/v1/is-open", 0.001, "Is an exchange open right now or at a given time"),
    ("market_next_open_close", "market-hours", "/v1/next", 0.001, "Next open and close of an exchange"),
    ("market_holidays", "market-hours", "/v1/holidays", 0.002, "Exchange holidays and early closes in a date range"),
    ("futures_front_month", "futures-expiry", "/v1/front-month", 0.001, "Front and most active futures contract"),
    ("futures_expirations", "futures-expiry", "/v1/expirations", 0.001, "Next futures expirations with roll dates"),
    ("futures_roll_calendar", "futures-expiry", "/v1/roll-calendar", 0.002, "Quarterly expirations and roll dates of a year"),
    ("settlement_date", "trading-time", "/v1/settlement-date", 0.001, "Settlement date of a stock trade (T+1/T+2)"),
    ("fx_sessions", "trading-time", "/v1/fx-sessions", 0.0011, "Open forex sessions, overlaps, next opens"),
    ("dst_gaps", "trading-time", "/v1/dst-gap", 0.0012, "Weeks when US/Europe clock differences shift"),
    ("is_dst_gap", "trading-time", "/v1/is-dst-gap", 0.0012, "Is a time inside a daylight-saving gap"),
    ("market_countdown", "trading-time", "/v1/countdown", 0.002, "Open/close countdown for several exchanges"),
    ("treasury_yield_curve", "macro-data", "/v1/yield-curve", 0.001, "US Treasury par yield curve, 2s10s, shape"),
    ("treasury_spread", "macro-data", "/v1/spread", 0.001, "Spread between two Treasury tenors"),
    ("ecb_fx_rate", "macro-data", "/v1/fx", 0.0011, "ECB euro reference rate incl. cross rates"),
    ("ecb_convert", "macro-data", "/v1/convert", 0.0011, "Convert an amount with ECB reference rates"),
    ("cot_positioning", "macro-data", "/v1/cot", 0.002, "CFTC Commitments of Traders positioning"),
    ("options_expiration_check", "market-calendar", "/v1/opex", 0.001, "Is a date an options expiration (US/VIX/Eurex)"),
    ("options_expiration_calendar", "market-calendar", "/v1/opex-calendar", 0.001, "Monthly expirations of a year"),
    ("vix_expirations", "market-calendar", "/v1/vix-expiry", 0.001, "Next VIX expirations"),
    ("economic_release_next", "market-calendar", "/v1/next-release", 0.0011, "Next CPI/NFP/PPI/GDP/PCE release"),
    ("economic_releases", "market-calendar", "/v1/releases", 0.0011, "US economic releases in a date range"),
    ("central_bank_next_meeting", "market-calendar", "/v1/next-meeting", 0.0012, "Next Fed/ECB decision with times"),
    ("central_bank_meetings", "market-calendar", "/v1/meetings", 0.0012, "Fed/ECB meetings of a year"),
    ("treasury_auctions", "market-calendar", "/v1/auctions", 0.0013, "US Treasury auctions and results"),
    ("treasury_next_auction", "market-calendar", "/v1/next-auction", 0.0013, "Next US Treasury auction"),
    ("is_banking_day", "market-calendar", "/v1/is-banking-day", 0.0014, "Banking day in TARGET2/US/CH"),
    ("banking_holidays", "market-calendar", "/v1/banking-holidays", 0.0014, "Banking holidays of a year"),
    ("index_rebalance_dates", "market-calendar", "/v1/rebalance", 0.002, "S&P/Nasdaq/Russell/DAX rebalancing"),
    ("sec_filings", "filings-specs", "/v1/filings", 0.002, "Latest SEC filings of a US ticker"),
    ("insider_transactions", "filings-specs", "/v1/insider", 0.002, "Parsed Form 4 insider transactions"),
    ("contract_spec", "filings-specs", "/v1/spec", 0.001, "Futures contract specification"),
    ("futures_tick_value", "filings-specs", "/v1/tick-value", 0.001, "Money value of a futures move"),
    ("futures_position_size", "filings-specs", "/v1/position-size", 0.001, "Contracts for a risk budget"),
]


# ================================================================ Bezahlung
UA = {"User-Agent": "market-data-mcp/0.1"}


class SpendLimitError(RuntimeError):
    pass


class Payer:
    def __init__(self, private_key: str | None = None, max_per_call: float | None = None,
                 max_per_session: float | None = None, http=None):
        self.private_key = private_key if private_key is not None else _key_from_env()
        self.max_per_call = max_per_call if max_per_call is not None else float(os.environ.get("MAX_USDC_PER_CALL", "0.01"))
        self.max_per_session = (max_per_session if max_per_session is not None
                                else float(os.environ.get("MAX_USDC_PER_SESSION", "0.10")))
        self.spent = 0.0
        self.calls = 0
        self._lock = threading.Lock()
        self._http = http or _clean_session(requests.Session())
        self._paid_session = None
        self.address = None
        if self.private_key:
            from eth_account import Account
            self.address = Account.from_key(self.private_key).address

    # -------------------------------------------------------------- Prüfung vor jeder Zahlung
    def check_requirement(self, req) -> float:
        """Prüft eine Zahlungsanforderung; gibt den Betrag in USDC zurück oder wirft SpendLimitError."""
        pay_to = str(getattr(req, "pay_to", "")).lower()
        network = str(getattr(req, "network", ""))
        asset = str(getattr(req, "asset", "")).lower()
        amount = int(getattr(req, "amount", 0)) / 1_000_000
        if pay_to not in ALLOWED_PAY_TO:
            raise SpendLimitError(f"Refused: payee {pay_to} is not a known Market Data API wallet")
        if network != NETWORK or asset != USDC:
            raise SpendLimitError("Refused: only USDC on Base mainnet is accepted")
        if amount > self.max_per_call:
            raise SpendLimitError(f"Refused: price {amount} USDC exceeds MAX_USDC_PER_CALL={self.max_per_call}")
        if self.spent + amount > self.max_per_session + 1e-12:
            raise SpendLimitError(f"Refused: session budget {self.max_per_session} USDC would be exceeded "
                                  f"(spent {self.spent:.4f})")
        return amount

    def _session(self):
        if self._paid_session is None:
            from eth_account import Account
            from x402 import x402ClientSync
            from x402.http.clients import x402_requests
            from x402.mechanisms.evm.exact import register_exact_evm_client
            from x402.schemas.hooks import AbortResult

            client = x402ClientSync()
            register_exact_evm_client(client, Account.from_key(self.private_key), networks=NETWORK)

            def guard(ctx):
                try:
                    amt = self.check_requirement(ctx.selected_requirements)
                except SpendLimitError as e:
                    return AbortResult(reason=str(e), message=str(e))
                self._pending = amt
                return None

            client.on_before_payment_creation(guard)
            self._client = client
            self._paid_session = _clean_session(x402_requests(client))
        return self._paid_session

    # -------------------------------------------------------------- Abruf
    def get(self, url: str, params: dict | None = None, paid: bool = True) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        with self._lock:
            if not paid:
                r = self._http.get(url, params=params, headers=UA, timeout=90)
                return _result(r)
            if not self.private_key:
                return {"error": "payment_not_configured",
                        "message": ("This tool costs a fraction of a cent in USDC on Base. Set X402_PRIVATE_KEY "
                                    "(or X402_KEY_FILE) for a small, dedicated wallet to enable automatic payment.")}
            self._pending = 0.0
            try:
                r = self._session().get(url, params=params, headers=UA, timeout=120)
            except Exception as e:  # Abbruch durch die Prüfung oder Netzfehler
                msg = str(e)
                return {"error": "payment_refused" if "Refused" in msg else "request_failed", "message": msg[:400]}
            out = _result(r)
            if r.status_code == 200 and r.headers.get("PAYMENT-RESPONSE"):
                self.spent += self._pending
                self.calls += 1
                out["_payment"] = {"usdc": self._pending, "session_spent_usdc": round(self.spent, 6),
                                   "session_budget_usdc": self.max_per_session}
            return out

    def status(self) -> dict:
        return {"payment_enabled": bool(self.private_key), "payer_address": self.address,
                "spent_usdc_this_session": round(self.spent, 6), "paid_calls": self.calls,
                "max_usdc_per_call": self.max_per_call, "max_usdc_per_session": self.max_per_session,
                "network": "Base mainnet (USDC)"}


def _clean_session(sess):
    """Ignore CA-bundle variables inherited from the host app (they can point to a bundle that lacks
    public CAs) and verify against certifi; on Windows/macOS also trust the system store if available."""
    sess.trust_env = False
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        import certifi
        sess.verify = certifi.where()
    return sess


def _key_from_env() -> str:
    key = os.environ.get("X402_PRIVATE_KEY", "").strip()
    path = os.environ.get("X402_KEY_FILE", "").strip()
    if key or not path:
        return key
    import json
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        text = f.read().strip()
    if text.startswith("{"):
        text = str(json.loads(text).get("private_key", "")).strip()
    return text


def _result(r) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {"text": r.text[:2000]}
    if r.status_code == 200:
        return body if isinstance(body, dict) else {"data": body}
    return {"error": f"http_{r.status_code}", "detail": body}


# ================================================================ MCP-Werkzeuge
mcp = FastMCP(
    "market-data",
    instructions=(
        "Trading-calendar, futures-expiry, settlement, forex-session, US Treasury yield curve, ECB FX and "
        "CFTC COT data. Paid tools cost 0.001-0.002 USDC per call, paid automatically via x402 on Base "
        "within the user's spending limits. Times are ISO 8601 with offset (e.g. 2026-11-27T15:00:00Z); "
        "dates are YYYY-MM-DD. Call list_markets / list_futures_products / list_cot_markets (free) for valid codes."),
)
payer: Payer | None = None


def _payer() -> Payer:
    global payer
    if payer is None:
        payer = Payer()
    return payer


def _get(api: str, path: str, paid: bool = True, **params) -> dict:
    return _payer().get(APIS[api] + path, params, paid=paid)


# ---------------------------------------------------------------- kostenlos
@mcp.tool()
def catalog_list() -> dict:
    """List all tools of this server with their price in USDC (free)."""
    return {"tools": [{"tool": t, "price_usdc": p, "description": d} for t, _, _, p, d in TOOLS],
            "payment": "x402, USDC on Base mainnet; paid automatically within MAX_USDC_PER_CALL / MAX_USDC_PER_SESSION"}


@mcp.tool()
def payment_status() -> dict:
    """Show whether payment is enabled, the payer address and how much was spent this session (free)."""
    return _payer().status()


@mcp.tool()
def list_markets() -> dict:
    """List supported exchanges and market codes such as XNYS, XETR, XSWX, CME_EQUITY (free)."""
    return _get("market-hours", "/v1/markets", paid=False)


@mcp.tool()
def list_futures_products() -> dict:
    """List supported futures products such as ES, MES, NQ, MNQ, YM, RTY, FDAX, FESX, FSMI (free)."""
    return _get("futures-expiry", "/v1/products", paid=False)


@mcp.tool()
def list_cot_markets() -> dict:
    """List futures markets available for cot_positioning (free)."""
    return _get("macro-data", "/v1/cot-markets", paid=False)


# ---------------------------------------------------------------- Market Hours API
@mcp.tool()
def market_is_open(market: str, at: Optional[str] = None) -> dict:
    """Is an exchange open now or at a given time? Includes holidays, early closes and lunch breaks.
    market: code like XNYS, XNAS, XETR, XSWX, XLON, CME_EQUITY. at: ISO time with offset (default now).
    Costs 0.001 USDC."""
    return _get("market-hours", "/v1/is-open", market=market, at=at)


@mcp.tool()
def market_next_open_close(market: str, at: Optional[str] = None) -> dict:
    """Next open and next close of an exchange after a given time (default now). Costs 0.001 USDC."""
    return _get("market-hours", "/v1/next", market=market, at=at)


@mcp.tool()
def market_holidays(market: str, start: str, end: str) -> dict:
    """Closed weekdays (holidays) and early closes of an exchange between start and end (YYYY-MM-DD).
    Costs 0.002 USDC."""
    return _get("market-hours", "/v1/holidays", market=market, start=start, end=end)


# ---------------------------------------------------------------- Futures Expiry API
@mcp.tool()
def futures_front_month(product: str, at: Optional[str] = None) -> dict:
    """Front contract and most active contract of a futures product (ES, NQ, FDAX, ...) at a time.
    Costs 0.001 USDC."""
    return _get("futures-expiry", "/v1/front-month", product=product, at=at)


@mcp.tool()
def futures_expirations(product: str, count: int = 4, at: Optional[str] = None) -> dict:
    """Next 1-12 contract expirations with last trading time and roll date. Costs 0.001 USDC."""
    return _get("futures-expiry", "/v1/expirations", product=product, count=count, at=at)


@mcp.tool()
def futures_roll_calendar(product: str, year: int) -> dict:
    """All quarterly expirations and roll dates of a futures product in a year. Costs 0.002 USDC."""
    return _get("futures-expiry", "/v1/roll-calendar", product=product, year=year)


# ---------------------------------------------------------------- Trading Time API
@mcp.tool()
def settlement_date(market: str, trade_date: str) -> dict:
    """Settlement date of a stock trade (T+1 US, T+2 elsewhere, holiday-aware). trade_date YYYY-MM-DD.
    Costs 0.001 USDC."""
    return _get("trading-time", "/v1/settlement-date", market=market, trade_date=trade_date)


@mcp.tool()
def fx_sessions(at: Optional[str] = None) -> dict:
    """Which forex sessions (Sydney, Tokyo, London, New York) are open, overlaps and next opens.
    Costs 0.0011 USDC."""
    return _get("trading-time", "/v1/fx-sessions", at=at)


@mcp.tool()
def dst_gaps(year: int, pair: str = "US-EU") -> dict:
    """Weeks in a year when the clock difference between the US and Europe shifts (DST gaps).
    pair: US-EU, US-UK or US-CH. Costs 0.0012 USDC."""
    return _get("trading-time", "/v1/dst-gap", year=year, pair=pair)


@mcp.tool()
def is_dst_gap(at: Optional[str] = None, pair: str = "US-EU") -> dict:
    """Is a time inside a daylight-saving gap between the US and Europe? Costs 0.0012 USDC."""
    return _get("trading-time", "/v1/is-dst-gap", at=at, pair=pair)


@mcp.tool()
def market_countdown(markets: str, at: Optional[str] = None) -> dict:
    """Time until open/close for several exchanges in one call. markets: comma list like XNYS,XETR,CME_EQUITY.
    Costs 0.002 USDC."""
    return _get("trading-time", "/v1/countdown", markets=markets, at=at)


# ---------------------------------------------------------------- Macro Data API
@mcp.tool()
def treasury_yield_curve(date: Optional[str] = None) -> dict:
    """US Treasury par yield curve (1M-30Y) on a date (default latest) with 2s10s, 3m10y and curve shape.
    Costs 0.001 USDC."""
    return _get("macro-data", "/v1/yield-curve", date=date)


@mcp.tool()
def treasury_spread(a: str = "2y", b: str = "10y", date: Optional[str] = None) -> dict:
    """Spread b minus a between two Treasury tenors (1m..30y) and whether it is inverted. Costs 0.001 USDC."""
    return _get("macro-data", "/v1/spread", a=a, b=b, date=date)


@mcp.tool()
def ecb_fx_rate(quote: str, base: str = "EUR", date: Optional[str] = None) -> dict:
    """ECB euro reference rate between two currencies (cross rates supported), e.g. base CHF quote USD.
    Costs 0.0011 USDC."""
    return _get("macro-data", "/v1/fx", base=base, quote=quote, date=date)


@mcp.tool()
def ecb_convert(amount: float, from_currency: str, to_currency: str, date: Optional[str] = None) -> dict:
    """Convert an amount between currencies with ECB reference rates. Costs 0.0011 USDC."""
    return _get("macro-data", "/v1/convert", amount=amount, **{"from": from_currency}, to=to_currency, date=date)


@mcp.tool()
def cot_positioning(market: str, weeks: int = 1) -> dict:
    """CFTC Commitments of Traders: commercial, non-commercial and small-trader positions and weekly change.
    market: ES, NQ, YM, RTY, GC, SI, CL, NG, 6E, 6J, 6B, 6S, ZN, BTC. weeks: 1-260 for history.
    Costs 0.002 USDC."""
    return _get("macro-data", "/v1/cot", market=market, weeks=weeks)


# ---------------------------------------------------------------- Market Calendar API
@mcp.tool()
def options_expiration_check(date: Optional[str] = None) -> dict:
    """Is a date (YYYY-MM-DD, default today) a US monthly options expiration, triple witching, weekly expiration,
    VIX expiration or Eurex expiration (holiday-adjusted)? Also returns the next ones. Costs 0.001 USDC."""
    return _get("market-calendar", "/v1/opex", date=date)


@mcp.tool()
def options_expiration_calendar(year: int, venue: str = "all") -> dict:
    """All monthly options expirations of a year. venue: all, us, vix or eurex. Costs 0.001 USDC."""
    return _get("market-calendar", "/v1/opex-calendar", year=year, venue=venue)


@mcp.tool()
def vix_expirations(count: int = 6, date: Optional[str] = None) -> dict:
    """Next 1-24 VIX futures/options expiration dates (Wednesday rule, holiday-adjusted). Costs 0.001 USDC."""
    return _get("market-calendar", "/v1/vix-expiry", count=count, date=date)


@mcp.tool()
def economic_release_next(indicator: str, date: Optional[str] = None) -> dict:
    """Next scheduled US release with time (ET and UTC) and reference period. indicator: CPI, NFP, PPI, GDP, PCE
    (comma list allowed). Costs 0.0011 USDC."""
    return _get("market-calendar", "/v1/next-release", indicator=indicator, date=date)


@mcp.tool()
def economic_releases(from_date: Optional[str] = None, to_date: Optional[str] = None,
                      indicator: Optional[str] = None) -> dict:
    """US economic releases (CPI, NFP, PPI, GDP, PCE) between two dates (YYYY-MM-DD, max 400 days).
    Costs 0.0011 USDC."""
    return _get("market-calendar", "/v1/releases", **{"from": from_date}, to=to_date, indicator=indicator)


@mcp.tool()
def central_bank_next_meeting(bank: str = "all", date: Optional[str] = None) -> dict:
    """Next Fed (FOMC) and/or ECB monetary policy decision with statement and press conference times.
    bank: fed, ecb or all. Costs 0.0012 USDC."""
    return _get("market-calendar", "/v1/next-meeting", bank=bank, date=date)


@mcp.tool()
def central_bank_meetings(year: int, bank: str = "all") -> dict:
    """All Fed (FOMC) and ECB monetary policy meetings of a year with times. Costs 0.0012 USDC."""
    return _get("market-calendar", "/v1/meetings", bank=bank, year=year)


@mcp.tool()
def treasury_auctions(from_date: Optional[str] = None, to_date: Optional[str] = None,
                      term: Optional[str] = None) -> dict:
    """US Treasury auctions (bills, notes, bonds) between two dates (max 92 days): upcoming announced auctions
    and results of past ones. term e.g. 10-Year, 13-Week. Costs 0.0013 USDC."""
    return _get("market-calendar", "/v1/auctions", **{"from": from_date}, to=to_date, term=term)


@mcp.tool()
def treasury_next_auction(term: Optional[str] = None) -> dict:
    """Next announced US Treasury auction, optionally for a term such as 2-Year or 13-Week. Costs 0.0013 USDC."""
    return _get("market-calendar", "/v1/next-auction", term=term)


@mcp.tool()
def is_banking_day(zone: str, date: Optional[str] = None) -> dict:
    """Is a date a banking/settlement day? zone: TARGET2 (euro payments), US (Fedwire) or CH (Switzerland/SIX).
    Returns the reason and the next banking day. Costs 0.0014 USDC."""
    return _get("market-calendar", "/v1/is-banking-day", zone=zone, date=date)


@mcp.tool()
def banking_holidays(zone: str, year: Optional[int] = None) -> dict:
    """Banking/settlement holidays of a year for TARGET2, US (Fedwire) or CH. Costs 0.0014 USDC."""
    return _get("market-calendar", "/v1/banking-holidays", zone=zone, year=year)


@mcp.tool()
def index_rebalance_dates(index: str = "ALL", year: Optional[int] = None) -> dict:
    """Index rebalancing/reconstitution dates (S&P 500, Nasdaq-100, Russell semi-annual, DAX) for a year:
    effective-after-close date and first trading day with new weights. index: SP500, NDX, RUSSELL, DAX, ALL.
    Costs 0.002 USDC."""
    return _get("market-calendar", "/v1/rebalance", index=index, year=year)


# ---------------------------------------------------------------- Filings & Specs API
@mcp.tool()
def sec_filings(ticker: str, form_type: Optional[str] = None, limit: int = 20) -> dict:
    """Latest SEC EDGAR filings of a US-listed company (8-K with item titles, 10-Q, 10-K, Form 4, S-1, 13D/G ...)
    with dates and direct links. form_type e.g. 8-K or "10-Q,10-K". Costs 0.002 USDC."""
    return _get("filings-specs", "/v1/filings", ticker=ticker, type=form_type, limit=limit)


@mcp.tool()
def insider_transactions(ticker: str, limit: int = 10) -> dict:
    """Recent Form 4 insider transactions of a US company, parsed: insider, role, purchase/sale/award, shares,
    price, value, holdings after, plus a purchase/sale summary. Costs 0.002 USDC."""
    return _get("filings-specs", "/v1/insider", ticker=ticker, limit=limit)


@mcp.tool()
def contract_spec(symbol: str) -> dict:
    """Futures contract specification (CME, CBOT, NYMEX, COMEX, Cboe, Eurex): multiplier, tick size, tick value,
    point value, currency, trading hours. symbol e.g. ES, MNQ, CL, GC, ZN, 6E, FDAX, FESX. Costs 0.001 USDC."""
    return _get("filings-specs", "/v1/spec", symbol=symbol)


@mcp.tool()
def futures_tick_value(symbol: str, ticks: Optional[float] = None, points: Optional[float] = None) -> dict:
    """Money value per contract of a move in ticks or points for a futures symbol. Costs 0.001 USDC."""
    return _get("filings-specs", "/v1/tick-value", symbol=symbol, ticks=ticks, points=points)


@mcp.tool()
def futures_position_size(symbol: str, risk: float, stop_ticks: Optional[float] = None,
                          stop_points: Optional[float] = None) -> dict:
    """How many futures contracts fit a risk budget (in contract currency) for a stop distance in ticks or points.
    Costs 0.001 USDC."""
    return _get("filings-specs", "/v1/position-size", symbol=symbol, risk=risk, stop_ticks=stop_ticks,
                stop_points=stop_points)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
