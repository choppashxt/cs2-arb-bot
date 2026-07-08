# Project brief — CS2 Skin Arbitrage Scanner

This file is context for Claude Code. Read it before making changes.

## What this is

A read-only price scanner for CS2 skins. It finds items you can BUY as a
listing on one platform and immediately SELL by filling a standing BUY ORDER
(bid) on another — selling into a live bid is an instant, guaranteed exit
instead of listing-and-waiting. It nets out the buy-order fill fee, filters for
spreads above a threshold, and posts them to Discord. **It reports
opportunities only — it does not buy, sell, place, or fill orders.** The human
executes trades manually.

## Current status

Working first version. Core logic is written and internally consistent, but it
has NOT been tested against the live APIs yet (it was written in a sandbox with
no network). Your first job is to get a real run working and fix anything the
live APIs disagree with.

## How to run

```bash
pip install -r requirements.txt
export CSFLOAT_API_KEY="..."          # required
export DISCORD_WEBHOOK_URL="..."      # optional; prints to console if unset
python scanner.py
```

## Architecture

Single file: `scanner.py`. Buy side = lowest listing; sell side = highest buy
order (bid). Data model: `Listing`, `BuyOrder`, `Opportunity` dataclasses.

Buy-side listing fetchers → `{name: Listing}`:
- `fetch_skinport_listings()` — public items feed, no auth. No per-listing lock
  info, so `tradable` is None (unknown). Cached ~5 min, rate-limited.
- `fetch_csfloat_listings()` — listings endpoint, API key. Captures listing id
  and `tradable`.
- `fetch_dmarket_listings()` — market items, Ed25519-signed. Needs both DMarket
  keys.

Sell-side buy-order fetchers → `{name: BuyOrder}` (highest bid + depth):
- `fetch_csfloat_buy_orders()` — per-item, uses CSFloat's INTERNAL buy-order
  endpoint (NOT in official docs). Gate: `CSFLOAT_BUY_ORDERS_ENABLED`.
- `fetch_dmarket_buy_orders()` — DMarket "targets-by-title", signed.
- Skinport has no public buy-order API → buy-side only. Buff163 excluded (no
  official public API — do NOT scrape).

- `find_opportunities()` — pairs every listing (buy A) against every buy order
  (sell B): `net = bid_B*(1-fee_B); spread = (net-listing_A)/listing_A`. Applies
  `MIN_SPREAD_PCT`, `MIN_ITEM_PRICE_USD`, `MIN_BUY_ORDER_DEPTH`, and the
  tradable-now gate on cross-platform opps. Classifies same-platform vs
  cross-platform; sorts same-platform first, then cross by spread.
- `_dmarket_signed_get()` — Ed25519 request signing (PyNaCl).
- `build_mock_data()` + `SCANNER_DRY_RUN=1` — offline pipeline test, no network.
- `send_discord_alert()` — posts top results, chunked under Discord's 2000-char limit.

Deployment: `.github/workflows/scan.yml` runs it every 15 min on GitHub Actions,
reading keys from repo secrets.

## NOT yet verified against live APIs

Egress was blocked when this was written, so the buy-order rework is UNVERIFIED
against live endpoints. Confirm on a real run with keys:
- CSFloat buy-order endpoint path + response shape (it's undocumented/internal).
- DMarket listing/target field names, price units (assumed cents), trade-lock
  field, and the buy-order fill fee.
Every parser degrades to "skip this source" on error, so a wrong guess yields
empty data rather than a crash.

## First tasks (do these before trusting output)

1. **Verify the CSFloat API call.** Confirm the endpoint, auth header format,
   pagination params (`page` vs cursor), and the price field/units against the
   current docs at https://csfloat.com/api/docs. Adjust `fetch_csfloat_prices()`
   to match. Handle 429s gracefully (there's a basic retry already).
2. **Verify the Skinport endpoint and fields** (`min_price`, `market_hash_name`)
   and confirm the rate limit so the 15-min schedule stays under it.
3. **Fix the Skinport item URLs.** They're currently guessed from the item name
   (`name.replace(' ', '-').lower()`) which won't resolve for names with `|`,
   `(`, `)`, etc. Either build the correct slug or switch to a search URL like
   `https://skinport.com/market?search=<url-encoded name>`.
4. **Confirm current fee percentages** for both platforms and update the
   constants at the top of `scanner.py`.
5. **Sanity-check a few flagged spreads by hand** on both sites before relying
   on it — make sure the "buy" and "sell" links point at the same item/wear.

## Design guardrails — keep these

- **Alert-only.** Do not add auto-buy, auto-sell, checkout automation, or
  anything that places orders. The value here is scanning; execution stays with
  the human.
- **No anti-detection.** Do not add CAPTCHA-solving, Steam trade-bot automation,
  proxy rotation to dodge rate limits, or anything designed to evade a
  platform's bot protection. Respect each API's rate limits and terms.
- **It's a hobby scanner, not financial advice.** Spreads carry real risk
  (price movement, thin liquidity, fills that don't happen).

## Optional ideas for later

- Float/pattern awareness (rare floats and patterns are mispriced vs base).
- Track spreads over time to see which items recur (SQLite or a CSV log).
- Add more read-only sources with public APIs (e.g. DMarket, Bitskins) behind
  the same fee-netting logic.
- A simple summary digest ("X opportunities today, best was Y%") instead of
  per-spread pings if the channel gets noisy.
