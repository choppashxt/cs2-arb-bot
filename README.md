# CS2 Skin Arbitrage Scanner

A read-only price scanner. It compares CS2 skin prices between **Skinport** and
**CSFloat**, subtracts each platform's selling fee, and posts any spread that
clears your threshold to a Discord channel.

It **reports opportunities only** — it does not buy, sell, or trade anything.
You execute manually.

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

| Variable             | Default | What it does                                        |
|----------------------|---------|-----------------------------------------------------|
| `CSFLOAT_API_KEY`    | —       | Required. Reads CSFloat listings.                   |
| `DISCORD_WEBHOOK_URL`| —       | Where alerts post. Omit to print to console only.   |
| `MIN_SPREAD_PCT`     | `15`    | Minimum net spread (after fees) to report.          |
| `MIN_ITEM_PRICE_USD` | `5`     | Ignore items cheaper than this.                     |

Fee percentages are set near the top of `scanner.py` (`SKINPORT_FEE_PCT`,
`CSFLOAT_FEE_PCT`) — update them if either platform changes its fees.

---

## Known limitations / rough edges

- Skinport item URLs come straight from the API's `item_page` field, so they
  resolve; if that field is ever missing the code falls back to a market
  search URL.
- CSFloat pagination uses their cursor-based scheme per the current docs
  (verified July 2026).
- Steam Community Market is intentionally excluded as a sell destination (its
  7-day trade lock kills the quick-flip model).
