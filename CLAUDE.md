# Project brief — CS2 Skin Arbitrage Scanner

This file is context for Claude Code. Read it before making changes.

## What this is

A read-only price scanner for CS2 skins. It finds items you can BUY as a
listing on one platform and SELL at an *achievable* price on another. The core
pricing rule: a seller's high ask is not money you can collect. Sell-side
proceeds are only ever (a) the highest active BUY ORDER minus the fill fee
(instant exit) or (b) the LOWEST current ask minus the listing fee, marked
"NOT INSTANT" (you'd undercut and wait). It applies false-spread guards
(outlier rejection vs a robust reference, implausible-spread cap, liquidity
floors, exact-item/phase matching, coin→cash normalization), and posts
survivors to Discord with a per-reason rejection summary. **It reports
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

Items are matched by a key = `market_hash_name` (+ ` · <phase>` for
Doppler-family items whose phase the API exposes; unknown-phase Doppler is
dropped). All fetchers and the dry-run fixtures ingest through
`ingest_listing()`, which enforces the condition checks and rejection counters.

Buy-side listing fetchers → `{key: Listing}`:
- `fetch_skinport_listings()` — public items feed, no auth. No per-listing lock
  info, so `tradable` is None (unknown). Cached ~5 min, rate-limited.
- `fetch_csfloat_listings()` — listings endpoint, API key. Captures listing id,
  `tradable`, Doppler phase, and `reference.predicted_price` (reference input).
- `fetch_dmarket_listings()` — market items, Ed25519-signed. Needs both DMarket
  keys. Observed asks feed the reference.
- `fetch_csgoroll_listings()` — UNOFFICIAL GraphQL read, OFF by default
  (`CSGOROLL_ENABLED`). Coin prices × `CSGOROLL_COIN_USD`, marked
  non-cash-equivalent, never a sell venue. See guardrails.
- `fetch_skinport_sales()` — Skinport sales-history feed → 7-day sales volume
  (`MIN_RECENT_SALES` gate) + completed-sales median (preferred reference).

Sell-side buy-order fetchers → `{key: BuyOrder}` (highest bid + depth):
- `fetch_csfloat_buy_orders()` — per-item, uses CSFloat's INTERNAL buy-order
  endpoint (NOT in official docs). Gate: `CSFLOAT_BUY_ORDERS_ENABLED`. Skips
  float/attribute-restricted orders (can't verify our item satisfies them).
- `fetch_dmarket_buy_orders()` — DMarket "targets-by-title", signed.
- Skinport has no public buy-order API → not-instant sell venue only. Buff163
  excluded (no official public API — do NOT scrape).

- `find_opportunities()` — per item: builds a robust reference (median of
  sales-median/platform-reference/ask inputs in `REF_INPUTS`), rejects any
  listing/bid deviating > `MAX_DEVIATION_PCT` from it, applies `MIN_RECENT_SALES`
  and `MIN_BUY_ORDER_DEPTH`, then pairs each surviving buy leg against
  achievable sell quotes: instant (top bid × (1-fill fee)) or not-instant
  (lowest ask × (1-listing fee), `ALLOW_NOT_INSTANT`). Spreads must land in
  [`MIN_SPREAD_PCT`, `MAX_PLAUSIBLE_SPREAD_PCT`]; higher = logged artifact.
  Tradable-now gate on cross-platform. Sort: instant same-platform, instant
  cross, not-instant; by spread within groups.
- `REJECTIONS`/`print_rejection_summary()` — every filtered opp is counted by
  reason (outlier_listing, outlier_bid, implausible_spread, thin_depth,
  low_sales, trade_locked, tradable_unknown, phase_unknown, condition_mismatch,
  conditional_order) and summarized each run with samples.
- `_dmarket_signed_get()` — Ed25519 request signing (PyNaCl).
- `build_mock_data()` + `SCANNER_DRY_RUN=1` — offline pipeline test exercising
  every accept path and every rejection counter (use `MIN_SPREAD_PCT=8`).
- `send_discord_alert()` — posts top results, chunked under Discord's 2000-char limit.

Deployment: `.github/workflows/scan.yml` runs it every 15 min on GitHub Actions,
reading keys from repo secrets.

## NOT yet verified against live APIs

Egress was blocked when this was written, so the rework is UNVERIFIED against
live endpoints. Confirm on a real run with keys:
- CSFloat buy-order endpoint path + response shape (it's undocumented/internal),
  including how restricted/conditional orders are represented.
- DMarket listing/target field names, price units (assumed cents), trade-lock
  field, and the target fill fee.
- Skinport `/v1/sales/history` field shape (`last_7_days.median` / `.volume`).
- CSGORoll GraphQL query/response shape (entirely best-effort, unofficial).
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
  proxy rotation to dodge rate limits, UA spoofing, or anything designed to
  evade a platform's bot protection. Respect each API's rate limits and terms.
  CSGORoll exception (owner-authorized, July 2026): the owner explicitly
  approved reading CSGORoll's unofficial GraphQL endpoint despite there being
  no official API. It stays OFF by default (`CSGOROLL_ENABLED`), uses a plain
  unspoofed request, and any block (401/403/429) means skip — never evade.
  Buff163 remains fully excluded: do NOT scrape it.
- **It's a hobby scanner, not financial advice.** Spreads carry real risk
  (price movement, thin liquidity, fills that don't happen).

## Optional ideas for later

- Float/pattern awareness (rare floats and patterns are mispriced vs base).
- Track spreads over time to see which items recur (SQLite or a CSV log).
- Add more read-only sources with public APIs (e.g. DMarket, Bitskins) behind
  the same fee-netting logic.
- A simple summary digest ("X opportunities today, best was Y%") instead of
  per-spread pings if the channel gets noisy.
