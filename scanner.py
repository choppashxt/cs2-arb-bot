"""
CS2 Skin Arbitrage Scanner — buy-order edition
----------------------------------------------
Finds items you can BUY as a listing on one platform and immediately SELL by
filling a standing BUY ORDER (bid) on another. Selling into a live buy order is
an instant, guaranteed exit (someone is already committed to buying at that
price) instead of listing and waiting for a buyer — so a flagged spread is one
you can actually realise now, not just on paper.

This tool ONLY finds and reports opportunities. It never places, fills, or
cancels an order. You execute every trade manually. (See CLAUDE.md guardrails.)

Model
    Buy side  (acquire): lowest current LISTING per item — Skinport, CSFloat, DMarket.
    Sell side (exit)   : highest active BUY ORDER per item — CSFloat, DMarket.
                         (Skinport has no public buy-order API, so it is a buy-side
                          source only. Buff163 has the deepest book but no official
                          public API — deliberately excluded, do NOT scrape it.)

Spread (per item, per direction):
    net_proceeds = highest_buy_order_B * (1 - buy_order_sell_fee_B)
    spread_pct   = (net_proceeds - lowest_listing_A) / lowest_listing_A * 100
Flagged when spread_pct >= MIN_SPREAD_PCT.

Two opportunity types:
    - cross-platform : buy listing on A, transfer, fill buy order on B. Needs the
                       bought item TRADABLE NOW to transfer in time (trade-locked
                       items can't be delivered before the order may vanish).
    - same-platform  : a listing priced below the top buy order on the SAME
                       platform — instantly fillable, no transfer. Rare, sniped
                       fast, but flagged first when it appears.

!! VERIFICATION STATUS !!
    This rewrite was authored without live network access to the three APIs
    (the sandbox blocks egress to csfloat.com, api.dmarket.com, api.skinport.com).
    Endpoint shapes below were taken from current public docs where they exist,
    and every parser is defensive + degrades to "skip this source" on error.
    Two things specifically MUST be confirmed against a live run with real keys:
      1. CSFloat's buy-order endpoint is NOT in CSFloat's official Slate docs
         (docs.csfloat.com only documents listings). The path used here is the
         community-known internal one. Confirm it works with your key, or set
         CSFLOAT_BUY_ORDERS_ENABLED=false to drop CSFloat as a sell venue.
      2. DMarket target (buy-order) fields, price units, trade-lock field, and
         the buy-order fill fee — see the DMarket section.
    Run `SCANNER_DRY_RUN=1 python scanner.py` to exercise the whole pipeline on
    built-in fixtures without touching the network.

Fees (SELLING into a buy order — confirm; the fill fee can differ from the
normal listing sell fee):
    - CSFloat: ~2% seller fee.
    - DMarket: ~5% seller fee (DMarket runs promos/reductions; verify yours).
    - Skinport 8% is irrelevant here — Skinport is buy-side only now.
"""

import os
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

try:
    # On Windows behind TLS-inspecting AV/proxies, Python's bundled CA store
    # rejects injected certs; truststore uses the OS store instead. Optional.
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

# ---------------------------------------------------------------------------
# CONFIG — edit these or set as environment variables
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


CSFLOAT_API_KEY = os.environ.get("CSFLOAT_API_KEY", "")

# DMarket needs BOTH a public key and an Ed25519 secret key (hex). Every DMarket
# request is signed. Leave blank to skip DMarket entirely.
DMARKET_PUBLIC_KEY = os.environ.get("DMARKET_PUBLIC_KEY", "")
DMARKET_SECRET_KEY = os.environ.get("DMARKET_SECRET_KEY", "")

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Minimum net spread (after fees) to report. Keep a buffer against price
# movement and other bots beating you to the listing.
MIN_SPREAD_PCT = float(os.environ.get("MIN_SPREAD_PCT", 15.0))

# Ignore items below this price — thin/low-value listings are noise and more
# likely to be stale or gone by the time you act.
MIN_ITEM_PRICE_USD = float(os.environ.get("MIN_ITEM_PRICE_USD", 5.0))

# CRITICAL FILTER. Only flag cross-platform opps where the item you'd buy is
# tradable immediately (no active trade lock/hold) — otherwise you can't
# transfer it to fill the buy order before the order may vanish.
REQUIRE_TRADABLE_NOW = _env_bool("REQUIRE_TRADABLE_NOW", True)

# Some feeds don't expose per-listing lock status (e.g. Skinport's aggregate
# items feed). When lock status is UNKNOWN and REQUIRE_TRADABLE_NOW is on, do we
# keep the opp (flagged "unknown") or drop it? Default: drop — "unknown" is not
# "confirmed tradable". Flip to true to keep unknowns (with a warning label).
TREAT_UNKNOWN_TRADABLE_AS_OK = _env_bool("TREAT_UNKNOWN_TRADABLE_AS_OK", False)

# Optional order-book depth gate. A lone high bid can be cancelled before you
# deliver; a wall of bids is safer. 0 disables the gate. Only applied when the
# platform actually reports depth.
MIN_BUY_ORDER_DEPTH = int(os.environ.get("MIN_BUY_ORDER_DEPTH", 0))

# Selling-into-a-buy-order fees (percent). Confirm — the fill fee can differ
# from the normal listing sell fee.
CSFLOAT_BUY_ORDER_FEE_PCT = float(os.environ.get("CSFLOAT_BUY_ORDER_FEE_PCT", 2.0))
DMARKET_BUY_ORDER_FEE_PCT = float(os.environ.get("DMARKET_BUY_ORDER_FEE_PCT", 5.0))

# CSFloat buy orders use an endpoint that isn't in CSFloat's official docs.
# Set false to keep CSFloat as a buy-side (listing) source but not a sell venue.
CSFLOAT_BUY_ORDERS_ENABLED = _env_bool("CSFLOAT_BUY_ORDERS_ENABLED", True)

# Buy-order lookups are per-item, so they're the expensive calls. Cap how many
# candidate items we look up per platform to stay well under rate limits.
MAX_BUY_ORDER_LOOKUPS = int(os.environ.get("MAX_BUY_ORDER_LOOKUPS", 60))

# Offline pipeline test with built-in fixtures (no network). See build_mock_data.
DRY_RUN = _env_bool("SCANNER_DRY_RUN", False)

DMARKET_GAME_ID = os.environ.get("DMARKET_GAME_ID", "a8db")  # a8db = CS:GO/CS2 on DMarket
DMARKET_BASE = "https://api.dmarket.com"

REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

# Sell fee per buy-order platform, keyed by the platform label used below.
SELL_FEE_PCT = {
    "CSFloat": CSFLOAT_BUY_ORDER_FEE_PCT,
    "DMarket": DMARKET_BUY_ORDER_FEE_PCT,
}


@dataclass
class Listing:
    """Cheapest current listing (the price you'd pay to acquire) on one platform."""
    platform: str
    name: str
    price: float
    url: str
    # True = tradable now, False = trade-locked/held, None = platform didn't say.
    tradable: Optional[bool] = None
    # CSFloat listing id, needed to look up that item's buy-order book.
    listing_id: Optional[str] = None


@dataclass
class BuyOrder:
    """Highest active buy order (bid) — the price you'd sell into — on one platform."""
    platform: str
    name: str
    price: float
    url: str
    depth: Optional[int] = None  # quantity behind the top bid, if the API reports it


@dataclass
class Opportunity:
    item_name: str
    buy_platform: str
    buy_price: float
    buy_url: str
    sell_platform: str
    buy_order_price: float          # gross top bid you'd fill
    sell_after_fee: float           # net proceeds after the fill fee
    sell_url: str
    spread_pct: float
    kind: str                       # "same-platform" | "cross-platform"
    tradable_now: Optional[bool] = None
    depth: Optional[int] = None


# ---------------------------------------------------------------------------
# Buy-side listing fetchers
# ---------------------------------------------------------------------------

def fetch_skinport_listings() -> dict:
    """
    {name: Listing} from Skinport's public, no-auth items feed (lowest listing
    per item). NOTE: this aggregate feed exposes no per-listing trade-lock info,
    so tradable stays None (unknown) — which, by default, filters Skinport out
    of cross-platform opps under REQUIRE_TRADABLE_NOW. That's intentional: we
    can't confirm the item is transferable in time.
    Docs: https://docs.skinport.com/  Rate limit: ~8 req / 5 min, cached ~5 min.
    """
    url = "https://api.skinport.com/v1/items"
    params = {"app_id": 730, "currency": "USD"}  # 730 = CS2/CS:GO
    headers = {"Accept-Encoding": "br"}  # Brotli required per Skinport docs
    resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    listings = {}
    for item in data:
        name = item.get("market_hash_name")
        price = item.get("min_price")
        if name and price:
            item_url = item.get("item_page") or ("https://skinport.com/market?search=" + quote(name))
            listings[name] = Listing("Skinport", name, float(price), item_url, tradable=None)
    return listings


def fetch_csfloat_listings() -> dict:
    """
    {name: Listing} from CSFloat's listings endpoint, lowest price per item.
    Captures the listing id (for buy-order lookups) and the `tradable` field.
    Docs: https://docs.csfloat.com/  Auth: `Authorization: <key>`. Prices in cents.
    """
    if not CSFLOAT_API_KEY:
        return {}

    url = "https://csfloat.com/api/v1/listings"
    headers = {"Authorization": CSFLOAT_API_KEY}
    params = {
        "sort_by": "lowest_price",
        "limit": 50,  # docs: max 50
        "min_price": int(MIN_ITEM_PRICE_USD * 100),  # cents; keeps penny listings out of the sort
    }

    listings = {}
    cursor = None
    retries_429 = 0
    pages_fetched = 0
    while pages_fetched < 10:  # a handful of pages is enough coverage
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            retries_429 += 1
            if retries_429 > 5:
                print("  CSFloat listings rate-limited after 5 retries — partial data.")
                break
            time.sleep(5 * retries_429)
            continue
        retries_429 = 0
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            rows = data.get("data", [])
            cursor = data.get("cursor")
        else:
            rows, cursor = data, None
        if not rows:
            break

        for row in rows:
            item = row.get("item", {})
            name = item.get("market_hash_name")
            price_cents = row.get("price")
            listing_id = row.get("id")
            if not (name and price_cents):
                continue
            price = price_cents / 100.0
            tradable = _csfloat_tradable(item)
            if name not in listings or price < listings[name].price:
                listings[name] = Listing(
                    "CSFloat", name, price,
                    f"https://csfloat.com/item/{listing_id}",
                    tradable=tradable, listing_id=listing_id,
                )
        pages_fetched += 1
        if not cursor:
            break
    return listings


def _csfloat_tradable(item: dict) -> Optional[bool]:
    """Interpret CSFloat's trade-lock signal. UNVERIFIED field semantics — CSFloat
    exposes `tradable` and may expose a lock-expiry timestamp. Treat a future
    lock expiry as locked; otherwise fall back to the `tradable` flag."""
    for key in ("tradable_at", "trade_lock_until", "lock_expires_at"):
        ts = item.get(key)
        if isinstance(ts, (int, float)) and ts > time.time():
            return False
    if "tradable" in item:
        return bool(item.get("tradable"))
    return None


def fetch_dmarket_listings() -> dict:
    """
    {name: Listing} from DMarket's market items (lowest offer per title).
    Signed request. Prices assumed in cents (USD). Trade-lock parsed defensively.
    Endpoint: GET /exchange/v1/market/items?gameId=<id>&orderBy=price&orderDir=asc
    UNVERIFIED against a live run — see module header.
    """
    if not (DMARKET_PUBLIC_KEY and DMARKET_SECRET_KEY):
        return {}

    listings = {}
    cursor = None
    pages = 0
    while pages < 10:
        q = (
            f"/exchange/v1/market/items?gameId={DMARKET_GAME_ID}"
            f"&currency=USD&limit=100&orderBy=price&orderDir=asc"
        )
        if cursor:
            q += f"&cursor={quote(cursor)}"
        data = _dmarket_signed_get(q)
        if data is None:
            break
        objects = data.get("objects", []) if isinstance(data, dict) else []
        cursor = data.get("cursor") if isinstance(data, dict) else None
        if not objects:
            break
        for obj in objects:
            name = obj.get("title")
            price = _dmarket_money_to_usd(obj.get("price"))
            if not (name and price):
                continue
            tradable = _dmarket_tradable(obj)
            item_id = obj.get("itemId") or obj.get("classId") or ""
            url = f"https://dmarket.com/ingame-items/item-list/csgo-skins?title={quote(name)}"
            if name not in listings or price < listings[name].price:
                listings[name] = Listing("DMarket", name, price, url, tradable=tradable)
        pages += 1
        if not cursor:
            break
    return listings


def _dmarket_tradable(obj: dict) -> Optional[bool]:
    """UNVERIFIED. DMarket exposes trade-lock via `extra`; try the common shapes."""
    extra = obj.get("extra") or {}
    for key in ("tradeLockDuration", "tradeLock", "lockDuration"):
        v = extra.get(key)
        if isinstance(v, (int, float)):
            return v <= 0
    if "tradable" in extra:
        return bool(extra.get("tradable"))
    lock = obj.get("lockStatus")
    if isinstance(lock, bool):
        return not lock
    return None


# ---------------------------------------------------------------------------
# Sell-side buy-order fetchers
# ---------------------------------------------------------------------------

def fetch_csfloat_buy_orders(listings: dict, candidates: list) -> dict:
    """
    {name: BuyOrder} — highest active CSFloat buy order per candidate item.

    !! Uses CSFloat's internal buy-order endpoint, which is NOT in the official
    Slate docs (community-known). Per-listing, so we only look up items we found
    a CSFloat listing for, capped at MAX_BUY_ORDER_LOOKUPS. Disable via
    CSFLOAT_BUY_ORDERS_ENABLED=false. UNVERIFIED — confirm response shape.
    """
    if not (CSFLOAT_API_KEY and CSFLOAT_BUY_ORDERS_ENABLED):
        return {}

    headers = {"Authorization": CSFLOAT_API_KEY}
    orders = {}
    looked_up = 0
    for name in candidates:
        if looked_up >= MAX_BUY_ORDER_LOOKUPS:
            break
        listing = listings.get(name)
        if not listing or not listing.listing_id:
            continue  # need a listing id to query that item's book
        looked_up += 1
        url = f"https://csfloat.com/api/v1/listings/{listing.listing_id}/buy-orders"
        try:
            resp = requests.get(url, headers=headers, params={"limit": 10}, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                time.sleep(3)
                continue
            resp.raise_for_status()
            body = resp.json()
        except (requests.RequestException, ValueError):
            continue
        rows = body.get("data", []) if isinstance(body, dict) else body
        top = _highest_buy_order(rows, price_key="price", qty_key="qty")
        if top:
            price_cents, depth = top
            orders[name] = BuyOrder(
                "CSFloat", name, price_cents / 100.0,
                url=listing.url, depth=depth,
            )
    return orders


def fetch_dmarket_buy_orders(candidates: list) -> dict:
    """
    {name: BuyOrder} — highest active DMarket target (buy order) per candidate.
    Endpoint: GET /marketplace-api/v1/targets-by-title/<gameId>/<title>
    Returns orders with `amount` (depth) and `price`. Signed request.
    UNVERIFIED against a live run — see module header.
    """
    if not (DMARKET_PUBLIC_KEY and DMARKET_SECRET_KEY):
        return {}

    orders = {}
    looked_up = 0
    for name in candidates:
        if looked_up >= MAX_BUY_ORDER_LOOKUPS:
            break
        looked_up += 1
        q = f"/marketplace-api/v1/targets-by-title/{DMARKET_GAME_ID}/{quote(name)}"
        data = _dmarket_signed_get(q)
        if not data:
            continue
        rows = data.get("orders", []) if isinstance(data, dict) else []
        best_price, best_depth = None, None
        for row in rows:
            price = _dmarket_money_to_usd(row.get("price"))
            if price is None:
                continue
            if best_price is None or price > best_price:
                best_price = price
                best_depth = row.get("amount")
        if best_price is not None:
            url = f"https://dmarket.com/ingame-items/item-list/csgo-skins?title={quote(name)}"
            depth = int(best_depth) if isinstance(best_depth, (int, float)) else None
            orders[name] = BuyOrder("DMarket", name, best_price, url=url, depth=depth)
    return orders


def _highest_buy_order(rows, price_key="price", qty_key="qty"):
    """From a list of buy-order dicts (prices in cents), return (top_price_cents,
    depth_at_top) or None. Highest price wins."""
    if not isinstance(rows, list):
        return None
    best = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        price = r.get(price_key)
        if not isinstance(price, (int, float)):
            continue
        if best is None or price > best[0]:
            qty = r.get(qty_key)
            best = (price, int(qty) if isinstance(qty, (int, float)) else None)
    return best


# ---------------------------------------------------------------------------
# DMarket request signing (Ed25519)
# ---------------------------------------------------------------------------

def _dmarket_signed_get(path_with_query: str):
    """Signed GET against DMarket. Returns parsed JSON, or None on any failure
    (missing crypto lib, network error, non-200, bad JSON) so one bad source
    never takes the whole scan down.

    Signing (per DMarket docs): sign the string
        method + path_with_query + body + timestamp
    with Ed25519 using the hex secret key; send:
        X-Api-Key, X-Request-Sign: "dmar ed25519 <hexsig>", X-Sign-Date: <ts>
    """
    try:
        from nacl.signing import SigningKey
        from nacl.encoding import HexEncoder
    except ImportError:
        print("  DMarket skipped: PyNaCl not installed (pip install PyNaCl).")
        return None
    try:
        ts = str(int(time.time()))
        string_to_sign = "GET" + path_with_query + "" + ts
        signing_key = SigningKey(DMARKET_SECRET_KEY.encode(), encoder=HexEncoder)
        signature = signing_key.sign(string_to_sign.encode()).signature.hex()
        headers = {
            "X-Api-Key": DMARKET_PUBLIC_KEY,
            "X-Request-Sign": "dmar ed25519 " + signature,
            "X-Sign-Date": ts,
            "Accept": "application/json",
        }
        resp = requests.get(DMARKET_BASE + path_with_query, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            time.sleep(3)
            return None
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  DMarket request failed ({e.__class__.__name__}).")
        return None


def _dmarket_money_to_usd(money) -> Optional[float]:
    """DMarket money -> USD float. Amounts are in cents. Accepts {'USD': '1234'},
    {'amount': '1234', 'currency': 'USD'}, or a bare number. UNVERIFIED units."""
    if money is None:
        return None
    raw = None
    if isinstance(money, dict):
        raw = money.get("USD", money.get("amount"))
    elif isinstance(money, (int, float, str)):
        raw = money
    try:
        return float(raw) / 100.0 if raw is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Core: pair listings against buy orders
# ---------------------------------------------------------------------------

def find_opportunities(listings_by_platform: dict, orders_by_platform: dict) -> list:
    """listings_by_platform: {platform: {name: Listing}}
       orders_by_platform:   {platform: {name: BuyOrder}}
    Pairs every listing (buy) against every buy order (sell) for the same item."""
    # Reindex by item name.
    listings_by_name = {}
    for plat, d in listings_by_platform.items():
        for name, lst in d.items():
            listings_by_name.setdefault(name, {})[plat] = lst
    orders_by_name = {}
    for plat, d in orders_by_platform.items():
        for name, order in d.items():
            orders_by_name.setdefault(name, {})[plat] = order

    opportunities = []
    for name in set(listings_by_name) & set(orders_by_name):
        for buy_plat, listing in listings_by_name[name].items():
            if listing.price < MIN_ITEM_PRICE_USD:
                continue
            for sell_plat, order in orders_by_name[name].items():
                fee = SELL_FEE_PCT.get(sell_plat, 0.0)
                net = order.price * (1 - fee / 100.0)
                spread = (net - listing.price) / listing.price * 100 if listing.price > 0 else 0
                if spread < MIN_SPREAD_PCT:
                    continue

                # Depth gate (only when depth is known).
                if MIN_BUY_ORDER_DEPTH and order.depth is not None and order.depth < MIN_BUY_ORDER_DEPTH:
                    continue

                same_platform = (buy_plat == sell_plat)

                # Tradable-now gate applies to cross-platform (needs a transfer).
                if not same_platform and REQUIRE_TRADABLE_NOW:
                    if listing.tradable is False:
                        continue
                    if listing.tradable is None and not TREAT_UNKNOWN_TRADABLE_AS_OK:
                        continue

                opportunities.append(Opportunity(
                    item_name=name,
                    buy_platform=buy_plat,
                    buy_price=listing.price,
                    buy_url=listing.url,
                    sell_platform=sell_plat,
                    buy_order_price=order.price,
                    sell_after_fee=net,
                    sell_url=order.url,
                    spread_pct=spread,
                    kind="same-platform" if same_platform else "cross-platform",
                    tradable_now=listing.tradable,
                    depth=order.depth,
                ))

    # Same-platform (fastest, no transfer) first, then cross-platform by spread.
    opportunities.sort(key=lambda o: (0 if o.kind == "same-platform" else 1, -o.spread_pct))
    return opportunities


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _tradable_label(o: Opportunity) -> str:
    if o.kind == "same-platform":
        # No cross-platform transfer, but a trade lock still delays delivery
        # into the buy order, so surface it when the platform told us.
        if o.tradable_now is False:
            return "same-platform (no transfer) — but item is trade-LOCKED, delivery delayed"
        return "same-platform (no transfer)"
    if o.tradable_now is True:
        return "tradable now: yes"
    if o.tradable_now is False:
        return "tradable now: NO (locked)"
    return "tradable now: unknown"


def _depth_label(o: Opportunity) -> str:
    return f", depth {o.depth}" if o.depth is not None else ""


def format_opportunity(o: Opportunity) -> str:
    tag = "SAME-PLATFORM MISPRICING" if o.kind == "same-platform" else "cross-platform"
    warn = "⚠️ " if o.tradable_now is False or (o.kind == "cross-platform" and o.tradable_now is not True) else ""
    return (
        f"[{tag}] {o.item_name} — {o.spread_pct:.1f}% net\n"
        f"Buy:  {o.buy_platform} @ ${o.buy_price:.2f} — {o.buy_url}\n"
        f"Sell into buy order: {o.sell_platform} @ ${o.buy_order_price:.2f} "
        f"(top bid{_depth_label(o)}) → net ${o.sell_after_fee:.2f} after fee — {o.sell_url}\n"
        f"{warn}{_tradable_label(o)}"
    )


def send_discord_alert(opportunities: list):
    if not opportunities:
        return
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set — console only.")
        return
    lines = ["**CS2 Arbitrage — sell-into-buy-order opportunities:**\n"]
    for o in opportunities[:15]:
        lines.append(format_opportunity(o) + "\n")
    content = "\n".join(lines)
    for i in range(0, len(content), 1900):  # Discord 2000-char limit
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": content[i:i + 1900]}, timeout=REQUEST_TIMEOUT)
        print(f"Discord webhook response: {resp.status_code}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def gather() -> tuple:
    """Fetch listings and buy orders from every enabled source. Each source is
    isolated: if one raises, we log and continue with the rest."""
    listings_by_platform = {}
    for label, fn in (
        ("Skinport", fetch_skinport_listings),
        ("CSFloat", fetch_csfloat_listings),
        ("DMarket", fetch_dmarket_listings),
    ):
        try:
            d = fn()
            if d:
                listings_by_platform[label] = d
                print(f"  {label} listings: {len(d)}")
        except requests.RequestException as e:
            print(f"  {label} listings failed ({e.__class__.__name__}) — skipping.")

    # Candidate items = anything we could buy at/above the price floor. We only
    # look up buy orders for these, which bounds the per-item calls.
    candidates = set()
    for d in listings_by_platform.values():
        for name, lst in d.items():
            if lst.price >= MIN_ITEM_PRICE_USD:
                candidates.add(name)
    candidates = sorted(candidates)

    orders_by_platform = {}
    csfloat_orders = fetch_csfloat_buy_orders(listings_by_platform.get("CSFloat", {}), candidates)
    if csfloat_orders:
        orders_by_platform["CSFloat"] = csfloat_orders
        print(f"  CSFloat buy orders: {len(csfloat_orders)}")
    dmarket_orders = fetch_dmarket_buy_orders(candidates)
    if dmarket_orders:
        orders_by_platform["DMarket"] = dmarket_orders
        print(f"  DMarket buy orders: {len(dmarket_orders)}")

    return listings_by_platform, orders_by_platform


def main():
    if DRY_RUN:
        print("DRY RUN — using built-in fixtures, no network calls.\n")
        listings_by_platform, orders_by_platform = build_mock_data()
    else:
        print("Fetching listings (buy side) and buy orders (sell side)...")
        listings_by_platform, orders_by_platform = gather()

    if not orders_by_platform:
        print("\nNo buy-order source returned data — nothing to sell into. "
              "Check CSFloat/DMarket keys, or run with SCANNER_DRY_RUN=1.")
        return

    opportunities = find_opportunities(listings_by_platform, orders_by_platform)
    if not opportunities:
        print(f"\nNo opportunities above {MIN_SPREAD_PCT}% net spread this run.")
        return

    same = [o for o in opportunities if o.kind == "same-platform"]
    cross = [o for o in opportunities if o.kind == "cross-platform"]
    print(f"\nFound {len(opportunities)} opportunities "
          f"({len(same)} same-platform, {len(cross)} cross-platform):\n")
    for o in opportunities[:15]:
        print(format_opportunity(o) + "\n")

    send_discord_alert(opportunities)


# ---------------------------------------------------------------------------
# Fixtures for the offline dry run (SCANNER_DRY_RUN=1)
# ---------------------------------------------------------------------------

def build_mock_data() -> tuple:
    """Synthetic data exercising every branch: same-platform mispricing,
    cross-platform tradable, cross-platform locked (filtered), unknown-tradable
    (filtered by default), and a depth-gated case."""
    listings_by_platform = {
        "Skinport": {
            # unknown tradable (Skinport feed) -> dropped from cross by default
            "AK-47 | Redline (Field-Tested)": Listing("Skinport", "AK-47 | Redline (Field-Tested)", 20.00, "https://skinport.com/x", tradable=None),
            "AWP | Asiimov (Field-Tested)":   Listing("Skinport", "AWP | Asiimov (Field-Tested)", 55.00, "https://skinport.com/y", tradable=None),
        },
        "CSFloat": {
            # cheap CSFloat listing + CSFloat buy order above it -> same-platform mispricing
            "AK-47 | Redline (Field-Tested)": Listing("CSFloat", "AK-47 | Redline (Field-Tested)", 18.50, "https://csfloat.com/item/1", tradable=True, listing_id="1"),
            # tradable -> valid cross-platform to DMarket
            "Glock-18 | Fade (Factory New)":  Listing("CSFloat", "Glock-18 | Fade (Factory New)", 400.00, "https://csfloat.com/item/2", tradable=True, listing_id="2"),
            # locked -> filtered out of cross-platform
            "M4A4 | Howl (Minimal Wear)":     Listing("CSFloat", "M4A4 | Howl (Minimal Wear)", 3000.00, "https://csfloat.com/item/3", tradable=False, listing_id="3"),
        },
        "DMarket": {
            "AWP | Asiimov (Field-Tested)":   Listing("DMarket", "AWP | Asiimov (Field-Tested)", 50.00, "https://dmarket.com/z", tradable=True),
        },
    }
    orders_by_platform = {
        "CSFloat": {
            # top bid above the CSFloat listing (18.50) -> same-platform mispricing
            "AK-47 | Redline (Field-Tested)": BuyOrder("CSFloat", "AK-47 | Redline (Field-Tested)", 23.00, "https://csfloat.com/item/1", depth=5),
            # bid for the locked Howl -> cross from CSFloat is filtered (locked)
            "M4A4 | Howl (Minimal Wear)":     BuyOrder("CSFloat", "M4A4 | Howl (Minimal Wear)", 3800.00, "https://csfloat.com/howl", depth=1),
        },
        "DMarket": {
            # high bid for Fade -> cross-platform CSFloat(buy)->DMarket(sell), tradable
            "Glock-18 | Fade (Factory New)":  BuyOrder("DMarket", "Glock-18 | Fade (Factory New)", 470.00, "https://dmarket.com/fade", depth=3),
            # bid for Asiimov -> cross DMarket(buy 50)->? only 55 Skinport listing, and DMarket listing 50; sell DMarket bid 60
            "AWP | Asiimov (Field-Tested)":   BuyOrder("DMarket", "AWP | Asiimov (Field-Tested)", 62.00, "https://dmarket.com/asiimov", depth=1),
        },
    }
    return listings_by_platform, orders_by_platform


if __name__ == "__main__":
    main()
