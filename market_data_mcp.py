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

__version__ = "0.1.1"

# ================================================================ Katalog
APIS = {
    "market-hours": "https://market-hours-api.onrender.com",
    "futures-expiry": "https://futures-expiry-api.onrender.com",
    "trading-time": "https://trading-time-api.onrender.com",
    "macro-data": "https://macro-data-api-313m.onrender.com",
}

# Nur an diese Wallets darf der Server zahlen (Schutz gegen umgeleitete Zahlungen).
ALLOWED_PAY_TO = {
    "0xb2b76adbc021dda03d2b04943079b9d174d5779f",  # Market Hours API
    "0xcb3ae5bf8b1b4b3fe9d987a9a733830095a2927b",  # Futures Expiry API
    "0x98ad309e7ea4ceec93ce61e7d89cf697c2f4fd10",  # Trading Time API
    "0x5d4201f59a0ac8bcdb6106752846a4993395ff31",  # Macro Data API
    "0xc577c27d10767abfc10d867b443577fe296e7d23",  # Market Calendar API (geplant)
    "0x6bffcc85172372ad4e9c1ffafe97980a2c778fc7",  # Filings & Specs API (geplant)
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
        self._http = http or requests.Session()
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
            self._paid_session = x402_requests(client)
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


def main():
    mcp.run()


if __name__ == "__main__":
    main()
