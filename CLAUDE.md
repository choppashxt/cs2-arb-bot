# Project brief — CS2 Skin Arbitrage Scanner

This file is context for Claude Code. Read it before making changes.

## What this is

A read-only price scanner comparing CS2 skin prices between Skinport and
CSFloat. It nets out each platform's selling fee, filters for spreads above a
threshold, and posts them to Discord. **It reports opportunities only — it does
not buy, sell, or trade.** The human executes trades manually.

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

Single file: `scanner.py`
- `fetch_skinport_prices()` — Skinport public items endpoint, no auth. Returns
  `{item_name: min_price}`. Note: this feed is cached (~5 min) and rate-limited.
- `fetch_csfloat_prices()` — CSFloat listings endpoint, needs an API key.
  Returns `{item_name: (lowest_price, listing_url)}`. Paginates a few pages.
- `find_opportunities()` — checks both directions (buy Skinport→sell CSFloat and
  vice versa), applies fees, filters by `MIN_SPREAD_PCT` and `MIN_ITEM_PRICE_USD`.
- `send_discord_alert()` — posts the top results to a Discord webhook, chunked
  to stay under Discord's 2000-char message limit.

Deployment: `.github/workflows/scan.yml` runs it every 15 min on GitHub Actions,
reading keys from repo secrets.

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
