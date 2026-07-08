"""
CS2 Skin Arbitrage Scanner
---------------------------
Compares live prices between Skinport and CSFloat, nets out each platform's
fees, and flags items where buying on one and selling on the other clears
your profit threshold.

This tool ONLY finds and reports opportunities. It does not place any
buy or sell orders — you execute manually.

Setup:
    1. pip install requests
    2. Get a free CSFloat API key: https://csfloat.com/api/docs (account settings)
    3. Set environment variables (see README.md) or edit the CONFIG section below.
    4. Run: python scanner.py

Notes on fees (update these if the platforms change their fee structure):
    - Skinport seller fee: 8% standard (6% for items >= 1000 EUR) since the
      July 2025 fee reduction. Buyer pays the listed price as-is.
    - CSFloat seller fee: ~2% standard tier. Buyer pays listed price as-is.
    - Steam Community Market fee: ~15% (13% Steam cut + 2% game cut), plus a
      7-day trade lock on newly acquired items before they can be re-listed.
      Steam is NOT included as a sell destination here because of the trade
      lock — it kills the "quick flip" model this bot is built for. You can
      add it back in if you're OK holding inventory for a week.
"""

import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

try:
    # On Windows machines behind TLS-inspecting antivirus/proxies, Python's
    # bundled CA store rejects the injected certificates. truststore makes
    # Python use the OS certificate store instead. Optional everywhere else.
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

# ---------------------------------------------------------------------------
# CONFIG — edit these or set as environment variables
# ---------------------------------------------------------------------------

CSFLOAT_API_KEY = os.environ.get("CSFLOAT_API_KEY", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Minimum net spread (after fees) to bother reporting. Set high (15-20%) so
# reported spreads have a buffer against price movement / other bots beating
# you to the listing.
MIN_SPREAD_PCT = float(os.environ.get("MIN_SPREAD_PCT", 15.0))

# Ignore items below this price — thin/low-value listings aren't worth the
# scan noise and are more likely to be stale or already gone by the time you act.
MIN_ITEM_PRICE_USD = float(os.environ.get("MIN_ITEM_PRICE_USD", 5.0))

SKINPORT_FEE_PCT = 8.0    # fee applied when SELLING on Skinport (6% for items >= 1000 EUR)
CSFLOAT_FEE_PCT = 2.0     # fee applied when SELLING on CSFloat

REQUEST_TIMEOUT = 20


@dataclass
class Opportunity:
    item_name: str
    buy_platform: str
    buy_price: float
    sell_platform: str
    sell_price_after_fee: float
    spread_pct: float
    buy_url: str
    sell_url: str
    # How far the buy-side listing sits below the buy platform's own market
    # reference price. None when the platform gave us no reference to compare
    # against; a negative value means the buy is ABOVE market (a red flag that
    # the spread is likely a stale/mismatched sell quote, not a real deal).
    buy_market_ref: Optional[float] = None
    buy_under_market_pct: Optional[float] = None


def fetch_skinport_prices() -> dict:
    """
    Returns {item_name: (price_usd, item_url, market_ref)} using Skinport's
    public, no-auth-required pricing endpoint. Skinport aggregates by item, so
    this gives the lowest current listing per item, the item's real page URL
    (the API returns `item_page`, so no slug guessing needed), and Skinport's
    own reference price (or None) for the under-market check.
    Docs: https://docs.skinport.com/items
    Rate limit: 8 requests / 5 min; the feed itself is cached ~5 min.
    """
    url = "https://api.skinport.com/v1/items"
    params = {"app_id": 730, "currency": "USD"}  # 730 = CS2/CS:GO
    # Brotli encoding is mandatory per Skinport's docs (needs the `Brotli`
    # package installed for requests to decode it).
    headers = {"Accept-Encoding": "br"}
    resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    prices = {}
    for item in data:
        name = item.get("market_hash_name")
        price = item.get("min_price")
        if name and price:
            item_url = item.get("item_page") or (
                "https://skinport.com/market?search=" + quote(name)
            )
            # Skinport's own reference for the item, used to flag under-market
            # buys. suggested_price is the headline reference; fall back to
            # median/mean if it's absent. None disables the flag for this item.
            ref = (
                item.get("suggested_price")
                or item.get("median_price")
                or item.get("mean_price")
            )
            market_ref = float(ref) if ref else None
            prices[name] = (float(price), item_url, market_ref)
    return prices


def fetch_csfloat_prices() -> dict:
    """
    Returns {item_name: (price_usd, listing_url, market_ref)} using CSFloat's
    listings endpoint, sorted by lowest price. market_ref is CSFloat's own
    reference price for that listing (or None), used for the under-market
    check. Requires an API key.
    Docs: https://csfloat.com/api/docs
    """
    if not CSFLOAT_API_KEY:
        raise RuntimeError("CSFLOAT_API_KEY not set — get one from your CSFloat account settings.")

    url = "https://csfloat.com/api/v1/listings"
    headers = {"Authorization": CSFLOAT_API_KEY}
    # min_price (in cents) matters: without it, lowest_price sort returns the
    # cheapest listings sitewide (penny stickers), which all fall below
    # MIN_ITEM_PRICE_USD and leave nothing to compare.
    params = {
        "sort_by": "lowest_price",
        "limit": 50,
        "min_price": int(MIN_ITEM_PRICE_USD * 100),
    }

    prices = {}
    cursor = None
    retries_429 = 0
    pages_fetched = 0
    # Paginate a handful of pages — enough coverage without hammering the API.
    # CSFloat uses cursor-based pagination: the response body is
    # {"data": [...], "cursor": "<opaque string for the next page>"}.
    while pages_fetched < 10:
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            retries_429 += 1
            if retries_429 > 5:
                print("  CSFloat rate limit persisted after 5 retries — stopping with partial data.")
                break
            time.sleep(5 * retries_429)
            continue
        retries_429 = 0
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            listings = data.get("data", [])
            cursor = data.get("cursor")
        else:
            # Bare-array response (per the older docs example): no cursor
            # available, so we can only take this one page.
            listings = data
            cursor = None
        if not listings:
            break

        for listing in listings:
            item = listing.get("item", {})
            name = item.get("market_hash_name")
            price_cents = listing.get("price")
            listing_id = listing.get("id")
            if name and price_cents:
                price = price_cents / 100.0
                # CSFloat's per-listing reference price (in cents) — the
                # platform's estimated market value, used to flag under-market
                # buys. Fall back to base_price if predicted_price is absent.
                reference = listing.get("reference") or {}
                ref_cents = reference.get("predicted_price") or reference.get("base_price")
                market_ref = ref_cents / 100.0 if ref_cents else None
                # Keep the lowest price seen per item name
                if name not in prices or price < prices[name][0]:
                    prices[name] = (price, f"https://csfloat.com/item/{listing_id}", market_ref)
        pages_fetched += 1
        if not cursor:
            break

    return prices


def _under_market_pct(price: float, market_ref: Optional[float]) -> Optional[float]:
    """How far below the platform's market reference `price` sits, as a
    percentage. None if there's no reference to compare against. A negative
    result means the price is ABOVE market — a flag that a flagged spread is
    probably a stale or mismatched quote rather than a genuine deal."""
    if not market_ref or market_ref <= 0:
        return None
    return (market_ref - price) / market_ref * 100


def find_opportunities(skinport_prices: dict, csfloat_prices: dict) -> list:
    opportunities = []

    all_items = set(skinport_prices.keys()) & set(csfloat_prices.keys())

    for name in all_items:
        sp_price, sp_url, sp_ref = skinport_prices[name]
        cf_price, cf_url, cf_ref = csfloat_prices[name]

        if sp_price < MIN_ITEM_PRICE_USD and cf_price < MIN_ITEM_PRICE_USD:
            continue

        # Direction 1: buy on CSFloat, sell on Skinport
        sell_after_fee = sp_price * (1 - SKINPORT_FEE_PCT / 100)
        spread = ((sell_after_fee - cf_price) / cf_price) * 100 if cf_price > 0 else 0
        if spread >= MIN_SPREAD_PCT:
            opportunities.append(Opportunity(
                item_name=name,
                buy_platform="CSFloat",
                buy_price=cf_price,
                sell_platform="Skinport",
                sell_price_after_fee=sell_after_fee,
                spread_pct=spread,
                buy_url=cf_url,
                sell_url=sp_url,
                buy_market_ref=cf_ref,
                buy_under_market_pct=_under_market_pct(cf_price, cf_ref),
            ))

        # Direction 2: buy on Skinport, sell on CSFloat
        sell_after_fee = cf_price * (1 - CSFLOAT_FEE_PCT / 100)
        spread = ((sell_after_fee - sp_price) / sp_price) * 100 if sp_price > 0 else 0
        if spread >= MIN_SPREAD_PCT:
            opportunities.append(Opportunity(
                item_name=name,
                buy_platform="Skinport",
                buy_price=sp_price,
                sell_platform="CSFloat",
                sell_price_after_fee=sell_after_fee,
                spread_pct=spread,
                buy_url=sp_url,
                sell_url=cf_url,
                buy_market_ref=sp_ref,
                buy_under_market_pct=_under_market_pct(sp_price, sp_ref),
            ))

    opportunities.sort(key=lambda o: o.spread_pct, reverse=True)
    return opportunities


def _under_market_label(o: Opportunity) -> str:
    """Human-readable under-market signal for one opportunity's buy leg."""
    if o.buy_under_market_pct is None:
        return "under market: n/a (no reference price)"
    pct = o.buy_under_market_pct
    if pct >= 0:
        return f"under market: {pct:.1f}% below {o.buy_platform} ref (${o.buy_market_ref:.2f})"
    return (
        f"ABOVE market: {abs(pct):.1f}% over {o.buy_platform} ref "
        f"(${o.buy_market_ref:.2f}) — verify listing"
    )


def send_discord_alert(opportunities: list):
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set — skipping Discord alert, printing to console only.")
        return

    if not opportunities:
        return

    lines = ["**CS2 Skin Arbitrage — opportunities found:**\n"]
    for o in opportunities[:15]:  # cap message size
        warn = "⚠️ " if (o.buy_under_market_pct is not None and o.buy_under_market_pct < 0) else ""
        lines.append(
            f"**{o.item_name}** — {o.spread_pct:.1f}% net spread\n"
            f"Buy: {o.buy_platform} @ ${o.buy_price:.2f} — {o.buy_url}\n"
            f"Sell: {o.sell_platform} @ ${o.sell_price_after_fee:.2f} (after fees) — {o.sell_url}\n"
            f"{warn}{_under_market_label(o)}\n"
        )
    content = "\n".join(lines)

    # Discord has a 2000 char limit per message; split if needed
    for i in range(0, len(content), 1900):
        chunk = content[i:i + 1900]
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk}, timeout=REQUEST_TIMEOUT)
        print(f"Discord webhook response: {resp.status_code}")


def main():
    print("Fetching Skinport prices...")
    skinport_prices = fetch_skinport_prices()
    print(f"  {len(skinport_prices)} items")

    print("Fetching CSFloat prices...")
    csfloat_prices = fetch_csfloat_prices()
    print(f"  {len(csfloat_prices)} items")

    print("Finding opportunities...")
    opportunities = find_opportunities(skinport_prices, csfloat_prices)

    if not opportunities:
        print(f"No opportunities found above {MIN_SPREAD_PCT}% net spread this run.")
        return

    print(f"\nFound {len(opportunities)} opportunities:\n")
    for o in opportunities[:15]:
        print(f"{o.item_name} | {o.spread_pct:.1f}% | Buy {o.buy_platform} ${o.buy_price:.2f} -> Sell {o.sell_platform} ${o.sell_price_after_fee:.2f} | {_under_market_label(o)}")

    send_discord_alert(opportunities)


if __name__ == "__main__":
    main()
