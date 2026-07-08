# CS2 Skin Arbitrage Scanner

A read-only price scanner. It finds items you can **buy as a listing** on one
platform and **sell at an achievable price** on another. Core rule: a seller's
high asking price is not money you can collect — sell-side proceeds are only
ever the **highest active buy order** minus the fill fee (**instant** exit), or
the **lowest current ask** minus the listing fee, clearly marked **NOT
INSTANT** (you'd undercut and wait for a buyer).

- **Buy side (acquire):** lowest listing — Skinport, CSFloat, DMarket, and
  optionally CSGORoll (unofficial endpoint, off by default, coin prices
  converted to cash and labeled).
- **Instant sell (exit):** highest buy order — CSFloat, DMarket.
- **Not-instant sell:** lowest ask — Skinport, CSFloat, DMarket.
- Buff163 is excluded — no official public API, and scraping it is off the table.

Every alert survives a set of **false-spread guards**: outlier rejection
against a robust per-item reference price, an implausible-spread cap, recent-
sales and order-depth liquidity floors, exact item/wear/StatTrak/Doppler-phase
matching, and coin→cash normalization. Each run prints a summary of how many
candidates were rejected and why.

It **reports opportunities only** — it does not buy, sell, place, or fill
orders. You execute manually.

---

## Honest caveats (read once)

- **Not "free money."** Reported spreads can shrink or vanish before you act —
  other bots hit the same listings, prices move, and a listing can be gone by
  the time you click. Treat the threshold as a buffer, not a guarantee.
- **Prices aren't real-time.** Skinport's public feed is cached (~5 min), so
  running more often than that doesn't help.
- **ToS / account risk.** This tool only *reads* public price data, which is
  low-risk. Actually trading based on it is your responsibility — automated
  *trading* on Steam is against its terms and can get accounts flagged. Keep
  this tool alert-only and do the buying/selling yourself.
- **Not financial advice.** It's a hobby scanner.

---

## What you need

1. Python 3.11+
2. A free **CSFloat API key** — from your CSFloat account settings
   (https://csfloat.com/api/docs). Skinport's price endpoint needs no key.
   **DMarket** is optional — set `DMARKET_PUBLIC_KEY` + `DMARKET_SECRET_KEY`
   (from DMarket account → Trading API) to include it; leave them blank to skip.
3. A **Discord webhook URL** for alerts (optional — without it, results just
   print to the console).
4. A **GitHub account** if you want it to run on a schedule for free.

---

## Quick start (local test)

```bash
# 1. Install the one dependency
pip install -r requirements.txt

# 2. Set your keys for this shell session
export CSFLOAT_API_KEY="your_key_here"
export DISCORD_WEBHOOK_URL="your_webhook_here"   # optional

# 3. Run it once
python scanner.py
```

Prefer a file? Copy `.env.example` to `.env`, fill it in, then load it:

```bash
set -a && source .env && set +a && python scanner.py
```

You should see it fetch both platforms and print any opportunities. If nothing
clears your threshold, that's normal — lower `MIN_SPREAD_PCT` temporarily to
confirm it's working end-to-end.

---

## Run it on autopilot (free, no server)

The repo includes a GitHub Actions workflow (`.github/workflows/scan.yml`) that
runs the scanner every 15 minutes.

1. Push this folder to a GitHub repo.
2. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**. Add:
   - `CSFLOAT_API_KEY`
   - `DISCORD_WEBHOOK_URL`
3. Go to the **Actions** tab and enable workflows if prompted. It'll start
   running on schedule; you can also trigger a manual run from there to test.

Free-tier minutes are plenty at a 15-minute interval. Make the repo **public**
if you want unlimited Actions minutes, or keep it private (2000 min/month is
still well within budget here).

---

## Getting a Discord webhook

In your Discord server: **Server Settings → Integrations → Webhooks → New
Webhook → Copy Webhook URL.** Paste it as `DISCORD_WEBHOOK_URL`.

---

## Configuration

All optional, set as environment variables or in `.env`:

| Variable                       | Default | What it does                                                        |
|--------------------------------|---------|--------------------------------------------------------------------|
| `CSFLOAT_API_KEY`              | —       | Reads CSFloat listings + buy orders.                               |
| `CSFLOAT_BUY_ORDERS_ENABLED`   | `true`  | Use CSFloat as an instant sell venue (endpoint is unofficial).     |
| `DMARKET_PUBLIC_KEY`           | —       | DMarket public key (optional; both DMarket keys needed).           |
| `DMARKET_SECRET_KEY`           | —       | DMarket Ed25519 secret key.                                        |
| `CSGOROLL_ENABLED`             | `false` | Read CSGORoll's unofficial endpoint (ToS risk — see .env.example). |
| `CSGOROLL_COIN_USD`            | `0.66`  | Cash value per CSGORoll coin for price conversion.                 |
| `DISCORD_WEBHOOK_URL`          | —       | Where alerts post. Omit to print to console only.                  |
| `MIN_SPREAD_PCT`               | `15`    | Minimum net spread (after fees) to report.                        |
| `MAX_PLAUSIBLE_SPREAD_PCT`     | `40`    | Spreads above this are logged as artifacts, never alerted.         |
| `MAX_DEVIATION_PCT`            | `25`    | Drop listings/bids deviating more than this from the reference.    |
| `MIN_ITEM_PRICE_USD`           | `5`     | Ignore items cheaper than this.                                    |
| `MIN_RECENT_SALES`             | `3`     | Require this many sales in 7 days (where sales data exists).       |
| `MIN_BUY_ORDER_DEPTH`          | `2`     | Require at least this many bids behind the top buy order.          |
| `REQUIRE_TRADABLE_NOW`         | `true`  | Only flag cross-platform opps whose bought item is tradable now.   |
| `TREAT_UNKNOWN_TRADABLE_AS_OK` | `false` | Keep opps where lock status is unknown (flagged) vs dropping them. |
| `ALLOW_NOT_INSTANT`            | `true`  | Include list-and-wait exits, marked "NOT INSTANT".                 |

Fees are env-configurable too: `CSFLOAT_BUY_ORDER_FEE_PCT`,
`DMARKET_BUY_ORDER_FEE_PCT` (instant fills) and `SKINPORT_SELL_FEE_PCT`,
`CSFLOAT_SELL_FEE_PCT`, `DMARKET_SELL_FEE_PCT` (not-instant listings). Run the
offline pipeline test any time with `SCANNER_DRY_RUN=1 MIN_SPREAD_PCT=8 python
scanner.py` — it exercises every accept path and rejection counter.

---

## Known limitations / rough edges

- Skinport item URLs come straight from the API's `item_page` field, so they
  resolve; if that field is ever missing the code falls back to a market
  search URL.
- CSFloat pagination uses their cursor-based scheme per the current docs
  (verified July 2026).
- CSFloat's **buy-order** endpoint is not in their official docs — it's the
  community-known internal one. Set `CSFLOAT_BUY_ORDERS_ENABLED=false` to use
  CSFloat for listings only.
- The buy-order rework has **not** been verified against live APIs yet (the
  sandbox blocked egress). Confirm CSFloat/DMarket buy-order fields, price
  units, trade-lock fields, and fill fees on a real run — see CLAUDE.md.
- Steam Community Market is intentionally excluded as a sell destination (its
  7-day trade lock kills the quick-flip model).
