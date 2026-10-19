"""Tests ohne Netz: Werkzeugliste, kostenlose Abfragen, Ausgabegrenzen und Empfänger-Prüfung."""
import asyncio
import types

import pytest

import market_data_mcp as server
from market_data_mcp import Payer, SpendLimitError
catalog = server

MAKRO = "0x5d4201f59a0Ac8bcdB6106752846a4993395FF31"


def req(amount=1000, pay_to=MAKRO, network="eip155:8453", asset=catalog.USDC):
    return types.SimpleNamespace(amount=str(amount), pay_to=pay_to, network=network, asset=asset)


def test_all_tools_registered():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {t for t, *_ in catalog.TOOLS} <= names
    assert {"catalog_list", "payment_status", "list_markets", "list_futures_products", "list_cot_markets"} <= names
    assert len(names) == len(catalog.TOOLS) + 5


def test_guard_accepts_known_wallet_and_price():
    assert Payer(private_key="", max_per_call=0.01, max_per_session=0.1).check_requirement(req()) == 0.001


@pytest.mark.parametrize("bad, msg", [
    (req(pay_to="0x000000000000000000000000000000000000dEaD"), "not a known"),
    (req(network="eip155:1"), "Base mainnet"),
    (req(asset="0x1111111111111111111111111111111111111111"), "Base mainnet"),
    (req(amount=50_000), "MAX_USDC_PER_CALL"),
])
def test_guard_refuses(bad, msg):
    with pytest.raises(SpendLimitError, match=msg):
        Payer(private_key="", max_per_call=0.01, max_per_session=0.1).check_requirement(bad)


def test_session_budget():
    p = Payer(private_key="", max_per_call=0.01, max_per_session=0.0025)
    p.spent = 0.002
    with pytest.raises(SpendLimitError, match="session budget"):
        p.check_requirement(req(1000))


class FakeResp:
    def __init__(self, status, js):
        self.status_code, self._js, self.headers, self.text = status, js, {}, ""

    def json(self):
        return self._js


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        return FakeResp(200, [{"market": "ES"}])


def test_free_tool_and_unconfigured_payment(monkeypatch):
    fake = FakeHttp()
    server.payer = Payer(private_key="", http=fake)
    out = server.list_cot_markets()
    assert out == {"data": [{"market": "ES"}]}
    assert fake.calls[0][0] == catalog.APIS["macro-data"] + "/v1/cot-markets"
    paid = server.treasury_yield_curve()
    assert paid["error"] == "payment_not_configured"
    assert len(fake.calls) == 1  # ohne Schlüssel wird nichts abgefragt
    st = server.payment_status()
    assert st["payment_enabled"] is False and st["spent_usdc_this_session"] == 0


def test_convert_maps_from_parameter():
    seen = {}

    class P(Payer):
        def get(self, url, params=None, paid=True):
            seen.update(url=url, params=params)
            return {}
    server.payer = P(private_key="")
    server.ecb_convert(100, "CHF", "USD")
    assert seen["params"]["from"] == "CHF" and seen["params"]["to"] == "USD"
    assert seen["url"].endswith("/v1/convert")


def test_guard_hook_inside_real_x402_client():
    """Die Prüfung hängt wirklich im x402-Client: falscher Empfänger -> keine Signatur."""
    from eth_account import Account
    from x402.schemas import PaymentRequired, PaymentRequirements, ResourceInfo

    p = Payer(private_key=Account.create().key.hex(), max_per_call=0.01, max_per_session=0.1)

    def pr(pay_to):
        return PaymentRequired(x402_version=2, accepts=[PaymentRequirements(
            scheme="exact", network="eip155:8453", asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            amount="1000", pay_to=pay_to, max_timeout_seconds=300,
            extra={"name": "USD Coin", "version": "2"})],
            resource=ResourceInfo(url="https://x/v1/a", description="", mime_type="application/json"))

    p._session()
    c = p._client
    ok = c.create_payment_payload(pr(MAKRO))
    assert ok.accepted.pay_to.lower() == MAKRO.lower() and p._pending == 0.001
    with pytest.raises(Exception, match="not a known"):
        c.create_payment_payload(pr("0x000000000000000000000000000000000000dEaD"))
