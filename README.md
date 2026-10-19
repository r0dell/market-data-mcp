# Market Data MCP

An MCP server that gives AI assistants and agents 33 paid market-data tools plus 5 free helper tools:

| Area | Tools | Price per call |
|---|---|---|
| Exchange hours | `market_is_open`, `market_next_open_close`, `market_holidays` | 0.001–0.002 USDC |
| Futures | `futures_front_month`, `futures_expirations`, `futures_roll_calendar` | 0.001–0.002 USDC |
| Trading time | `settlement_date`, `fx_sessions`, `dst_gaps`, `is_dst_gap`, `market_countdown` | 0.001–0.002 USDC |
| Macro | `treasury_yield_curve`, `treasury_spread`, `ecb_fx_rate`, `ecb_convert`, `cot_positioning` | 0.001–0.002 USDC |
| Calendars | `options_expiration_check`, `options_expiration_calendar`, `vix_expirations`, `economic_release_next`, `economic_releases`, `central_bank_next_meeting`, `central_bank_meetings`, `treasury_auctions`, `treasury_next_auction`, `is_banking_day`, `banking_holidays`, `index_rebalance_dates` | 0.001–0.002 USDC |
| Filings & specs | `sec_filings`, `insider_transactions`, `contract_spec`, `futures_tick_value`, `futures_position_size` | 0.001–0.002 USDC |
| Free | `catalog_list`, `payment_status`, `list_markets`, `list_futures_products`, `list_cot_markets` | free |

Exchanges: NYSE, Nasdaq, LSE, Xetra, SIX, Tokyo, Hong Kong, Shanghai, ASX, CME equity futures.
Futures: ES, MES, NQ, MNQ, YM, MYM, RTY, M2K, FDAX, FDXM, FESX, FSMI.
Macro data comes from the U.S. Treasury (public domain), the ECB (euro reference rates; source: ECB statistics)
and the CFTC Commitments of Traders report (public domain).

## How payment works

Each paid tool calls an HTTP API protected by [x402](https://x402.org). The server pays automatically in
USDC on Base, using a wallet key you provide, and **only**:

- to the known wallets of these APIs (any other payee is refused),
- in USDC on Base mainnet,
- up to `MAX_USDC_PER_CALL` per call (default 0.01) and `MAX_USDC_PER_SESSION` per server start (default 0.10).

Without a key the free tools work and paid tools explain how to enable payment.

> Use a **separate, small wallet** for `X402_PRIVATE_KEY` with only a few USDC on Base. Never use your main wallet.
> No ETH is needed for gas; the x402 facilitator pays it.

## Install (Claude Desktop, Cursor, any MCP client)

Requires [uv](https://docs.astral.sh/uv/). Add this to your MCP client config
(Claude Desktop: Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "market-data": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/r0dell/market-data-mcp", "market-data-mcp"],
      "env": {
        "X402_KEY_FILE": "C:\\path\\to\\wallet.json",
        "MAX_USDC_PER_SESSION": "0.10"
      }
    }
  }
}
```

`X402_KEY_FILE` points to a file with the key (plain text or JSON with `private_key`);
alternatively set `X402_PRIVATE_KEY` directly. Without uv: `pip install https://github.com/r0dell/market-data-mcp/archive/refs/heads/main.zip`
and use `"command": "python", "args": ["-m", "market_data_mcp"]`.

Then ask for example: *"Is the NYSE open on 27 November 2026 at 18:00 UTC?"*,
*"What is the ES roll date in December?"* or *"Show the current US yield curve and the 2s10s spread."*

## Direct HTTP use

The same data is available as x402 HTTP APIs, listed in the Coinbase x402 Bazaar:

- https://market-hours-api.onrender.com
- https://futures-expiry-api.onrender.com
- https://trading-time-api.onrender.com
- https://macro-data-api-313m.onrender.com
- https://market-calendar-api.onrender.com
- https://filings-specs-api.onrender.com

## Disclaimer

Data is provided as is, without warranty. Not investment advice. Always confirm critical dates with the exchange.

License: MIT
