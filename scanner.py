"""
CS2 Skin Arbitrage Scanner — buy-order edition, with false-spread guards
------------------------------------------------------------------------
Finds items you can BUY as a listing on one platform and SELL for real money on
another, and only reports spreads you could actually transact at.

The core correction (v3): a seller's high ASKING price is not money you can
collect. Achievable sell-side proceeds are only ever one of:
  (a) INSTANT   — fill the highest active buy order (bid), minus that
                  platform's buy-order fill fee; or
  (b) NOT-INSTANT — undercut/match the LOWEST current listing and wait for a
                  buyer, minus the normal listing sale fee. Marked "not
                  instant" because it needs a buyer to show up.
The scanner never uses any ask above the lowest one as proceeds, and it never
alerts on numbers that look like data artifacts (see guards below).

This tool ONLY finds and reports opportunities. It never places, fills, or
cancels an order. You execute every trade manually. (See CLAUDE.md guardrails.)

Sources
    Buy side (listings): Skinport, CSFloat, DMarket, CSGORoll (optional, see below).
    Sell side:
      instant     — CSFloat buy orders, DMarket targets.
      not-instant — lowest listing on Skinport / CSFloat / DMarket.
    Buff163: excluded — no official public API (per CLAUDE.md, not scraped).
    CSGORoll: has NO official public API. The owner explicitly authorized a
      best-effort read of the unofficial GraphQL endpoint the site's own web
      client uses. It is OFF by default (CSGOROLL_ENABLED). Prices are in site
      COINS, not cash — they are converted via CSGOROLL_COIN_USD and marked
      non-cash-equivalent; CSGORoll is never used as a sell venue. No
      anti-detection of any kind: plain requests, no UA spoofing, no proxies;
      if the site blocks the call we skip it. Expect ToS risk.

False-spread guards (each rejection is counted and summarized per run)
    1. Robust reference price per item: median of {recent-sales median (Skinport
       sales feed), platform reference prices, observed asks}. Any single
       listing or bid deviating > MAX_DEVIATION_PCT from the reference is
       discarded before spread math (kills the one-overpriced-listing problem).
    2. Sanity cap: computed spreads above MAX_PLAUSIBLE_SPREAD_PCT are logged
       as rejected, never alerted — real cross-market spreads are single to
       low-double digits.
    3. Liquidity floors: MIN_RECENT_SALES (where sales data exists) and
       MIN_BUY_ORDER_DEPTH (a lone qty-1 bid is bait, not a market).
    4. Exact-item matching: market_hash_name (encodes wear + StatTrak™ +
       Souvenir) plus Doppler/Gamma Doppler PHASE where the API exposes it.
       Doppler-family items with unknown phase are dropped (phases price wildly
       differently). Platform StatTrak/Souvenir flags are cross-checked against
       the name. CSFloat buy orders with float/attribute restrictions are
       skipped (can't verify our item satisfies them).
    5. Cash normalization: coin/credit sites (CSGORoll) are converted to cash
       value and labeled; non-cash platforms never count as a cash exit.

Spread (per item, buy leg A -> sell quote B):
    net_proceeds = bid_B * (1 - buy_order_fee_B)          [instant]
                 | lowest_ask_B * (1 - listing_fee_B)      [not-instant]
    spread_pct   = (net_proceeds - listing_A) / listing_A * 100
Flagged when MIN_SPREAD_PCT <= spread_pct <= MAX_PLAUSIBLE_SPREAD_PCT.

Cross-platform opportunities require the bought item to be TRADABLE NOW
(REQUIRE_TRADABLE_NOW) — a trade-locked item can't be transferred in time.

!! VERIFICATION STATUS !!
    Written without live network access (sandbox blocks egress to all market
    APIs). Endpoint shapes follow current public docs where they exist; every
    parser degrades to "skip this source" on error. MUST be confirmed live:
      - CSFloat buy-order endpoint (internal, not in official docs) + fields.
      - DMarket field names, price units (assumed cents), trade-lock field,
        target fill fee.
      - Skinport /v1/sales/history field shape (documented, but unverified).
      - CSGORoll GraphQL query shape (unofficial, entirely best-effort).
    Offline pipeline test: SCANNER_DRY_RUN=1 MIN_SPREAD_PCT=8 python scanner.py
"""

import os
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
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

# DMarket needs BOTH a public key and an Ed25519 secret key (hex). Every
# DMarket request is signed. Leave blank to skip DMarket entirely.
DMARKET_PUBLIC_KEY = os.environ.get("DMARKET_PUBLIC_KEY", "")
DMARKET_SECRET_KEY = os.environ.get("DMARKET_SECRET_KEY", "")

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Report window: at least MIN_SPREAD_PCT, and no more than
# MAX_PLAUSIBLE_SPREAD_PCT (above that it's almost certainly a data artifact).
MIN_SPREAD_PCT = float(os.environ.get("MIN_SPREAD_PCT", 15.0))
MAX_PLAUSIBLE_SPREAD_PCT = float(os.environ.get("MAX_PLAUSIBLE_SPREAD_PCT", 40.0))

# Discard any individual listing/bid deviating more than this from the item's
# robust reference price, before any spread math.
MAX_DEVIATION_PCT = float(os.environ.get("MAX_DEVIATION_PCT", 25.0))

# Ignore items below this price — thin/low-value listings are noise.
MIN_ITEM_PRICE_USD = float(os.environ.get("MIN_ITEM_PRICE_USD", 5.0))

# Liquidity floors.
MIN_RECENT_SALES = int(os.environ.get("MIN_RECENT_SALES", 3))       # sales/7d, where data exists
MIN_BUY_ORDER_DEPTH = int(os.environ.get("MIN_BUY_ORDER_DEPTH", 2))  # top-bid quantity; qty-1 = bait

# CRITICAL: only flag cross-platform opps where the bought item is tradable
# immediately — a locked item can't be transferred to fill the order in time.
REQUIRE_TRADABLE_NOW = _env_bool("REQUIRE_TRADABLE_NOW", True)
# When a feed doesn't expose lock status (Skinport aggregate, CSGORoll), keep
# the opp (labeled "unknown") or drop it? Default drop — unknown != tradable.
TREAT_UNKNOWN_TRADABLE_AS_OK = _env_bool("TREAT_UNKNOWN_TRADABLE_AS_OK", False)

# Include not-instant (list-and-wait) sell quotes, clearly marked.
ALLOW_NOT_INSTANT = _env_bool("ALLOW_NOT_INSTANT", True)

# Fees (percent). Confirm against your account tier — the buy-order fill fee
# can differ from the normal listing sale fee.
BUY_ORDER_FEE_PCT = {
    "CSFloat": float(os.environ.get("CSFLOAT_BUY_ORDER_FEE_PCT", 2.0)),
    "DMarket": float(os.environ.get("DMARKET_BUY_ORDER_FEE_PCT", 5.0)),
}
LISTING_FEE_PCT = {
    "Skinport": float(os.environ.get("SKINPORT_SELL_FEE_PCT", 8.0)),  # 6% >= 1000 EUR
    "CSFloat": float(os.environ.get("CSFLOAT_SELL_FEE_PCT", 2.0)),
    "DMarket": float(os.environ.get("DMARKET_SELL_FEE_PCT", 5.0)),
}

# CSFloat buy orders use an endpoint that isn't in CSFloat's official docs.
CSFLOAT_BUY_ORDERS_ENABLED = _env_bool("CSFLOAT_BUY_ORDERS_ENABLED", True)

# CSGORoll (unofficial, owner-authorized — see module docstring). Off by default.
CSGOROLL_ENABLED = _env_bool("CSGOROLL_ENABLED", False)
# Cash value of one CSGORoll coin. Coins can't be withdrawn as money — you exit
# by withdrawing a skin and reselling it — so their real cash value sits well
# below 1:1. Set this to YOUR realistic acquisition/exit rate.
CSGOROLL_COIN_USD = float(os.environ.get("CSGOROLL_COIN_USD", 0.66))

# Buy-order lookups are per-item (the expensive calls) — cap them per platform.
MAX_BUY_ORDER_LOOKUPS = int(os.environ.get("MAX_BUY_ORDER_LOOKUPS", 60))

# Offline pipeline test with built-in fixtures (no network).
DRY_RUN = _env_bool("SCANNER_DRY_RUN", False)

DMARKET_GAME_ID = os.environ.get("DMARKET_GAME_ID", "a8db")  # a8db = CS:GO/CS2
DMARKET_BASE = "https://api.dmarket.com"

REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Data model + run-wide state
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    """Cheapest current listing per (platform, item) — the price to acquire."""
    platform: str
    key: str                     # match key: market_hash_name (+ phase suffix)
    price: float                 # USD cash-equivalent
    url: str
    tradable: Optional[bool] = None   # True/False/None(unknown)
    listing_id: Optional[str] = None  # CSFloat id, for buy-order lookups
    cash_equivalent: bool = True      # False for coin/credit-priced sites


@dataclass
class BuyOrder:
    """Highest active unconditional buy order (bid) per (platform, item)."""
    platform: str
    key: str
    price: float
    url: str
    depth: Optional[int] = None  # quantity behind the top bid, if reported


@dataclass
class SellQuote:
    """An achievable exit: fill a bid (instant) or undercut the lowest ask
    (not instant). Never derived from any ask above the lowest."""
    platform: str
    gross: float
    net: float
    url: str
    instant: bool
    depth: Optional[int] = None


@dataclass
class Opportunity:
    item_key: str
    buy_platform: str
    buy_price: float
    buy_url: str
    sell: SellQuote
    spread_pct: float
    kind: str                    # "same-platform" | "cross-platform"
    tradable_now: Optional[bool] = None
    reference: Optional[float] = None
    coin_note: str = ""          # set when the buy leg is coin-denominated


# Run-wide accumulators (reset in main()).
REJECTIONS = Counter()
REJECT_SAMPLES = defaultdict(list)
REF_INPUTS = defaultdict(list)   # key -> [candidate reference prices]
SALES_7D = {}                    # market_hash_name -> sales volume last 7 days

REJECT_REASONS = (
    "outlier_listing", "outlier_bid", "implausible_spread", "thin_depth",
    "low_sales", "trade_locked", "tradable_unknown", "phase_unknown",
    "condition_mismatch", "conditional_order",
)


def reject(reason: str, key: str = "", detail: str = ""):
    REJECTIONS[reason] += 1
    if key and len(REJECT_SAMPLES[reason]) < 3:
        REJECT_SAMPLES[reason].append(f"{key}{f' ({detail})' if detail else ''}")


def reference_price(key: str) -> Optional[float]:
    vals = REF_INPUTS.get(key)
    return statistics.median(vals) if vals else None


def deviates(price: float, ref: Optional[float]) -> bool:
    if not ref or ref <= 0:
        return False  # no reference -> can't judge, don't reject on this axis
    return abs(price - ref) / ref * 100 > MAX_DEVIATION_PCT


def base_name(key: str) -> str:
    return key.split(" · ")[0]


def make_match_key(name: str, phase: Optional[str]) -> Optional[str]:
    """Match items exactly. market_hash_name already encodes wear, StatTrak™
    and Souvenir. Doppler/Gamma Doppler phases share one name but price wildly
    differently — phase becomes part of the key; unknown phase = unmatchable."""
    if phase:
        return f"{name} · {phase}"
    if "Doppler" in name:  # covers Gamma Doppler too
        return None
    return name


def ingest_listing(store: dict, platform: str, name: str, price: float, url: str,
                   tradable: Optional[bool] = None, listing_id: Optional[str] = None,
                   phase: Optional[str] = None, is_stattrak: Optional[bool] = None,
                   is_souvenir: Optional[bool] = None, cash_equivalent: bool = True,
                   ref_input: Optional[float] = None):
    """Shared ingestion for real fetchers AND dry-run fixtures, so condition
    checks and rejection counters behave identically in both."""
    if is_stattrak is not None and is_stattrak != ("StatTrak™" in name):
        reject("condition_mismatch", name, "StatTrak flag vs name")
        return
    if is_souvenir is not None and is_souvenir != ("Souvenir" in name):
        reject("condition_mismatch", name, "Souvenir flag vs name")
        return
    key = make_match_key(name, phase)
    if key is None:
        reject("phase_unknown", name)
        return
    if ref_input:
        REF_INPUTS[key].append(float(ref_input))
    if key not in store or price < store[key].price:
        store[key] = Listing(platform, key, price, url, tradable=tradable,
                             listing_id=listing_id, cash_equivalent=cash_equivalent)


# ---------------------------------------------------------------------------
# Buy-side listing fetchers
# ---------------------------------------------------------------------------

def fetch_skinport_listings() -> dict:
    """{key: Listing} from Skinport's public items feed (lowest ask per item).
    No per-listing lock info -> tradable None. median_price feeds the reference.
    Doppler-family items are dropped (feed can't distinguish phases).
    Docs: https://docs.skinport.com/  Rate limit ~8 req/5min, cached ~5 min."""
    url = "https://api.skinport.com/v1/items"
    params = {"app_id": 730, "currency": "USD"}
    headers = {"Accept-Encoding": "br"}  # Brotli required per Skinport docs
    resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    store = {}
    for item in resp.json():
        name = item.get("market_hash_name")
        price = item.get("min_price")
        if not (name and price):
            continue
        page = item.get("item_page") or ("https://skinport.com/market?search=" + quote(name))
        ref = item.get("median_price") or item.get("suggested_price") or item.get("mean_price")
        ingest_listing(store, "Skinport", name, float(price), page,
                       tradable=None, ref_input=float(ref) if ref else None)
    return store


def fetch_skinport_sales() -> None:
    """Populate SALES_7D and reference inputs from Skinport's aggregated sales
    history (completed sales — the preferred reference per item).
    Docs: https://docs.skinport.com/sales/history  UNVERIFIED field shape."""
    url = "https://api.skinport.com/v1/sales/history"
    params = {"app_id": 730, "currency": "USD"}
    headers = {"Accept-Encoding": "br"}
    resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    for row in resp.json():
        name = row.get("market_hash_name")
        week = row.get("last_7_days") or {}
        if not name or not isinstance(week, dict):
            continue
        vol = week.get("volume")
        if isinstance(vol, (int, float)):
            SALES_7D[name] = int(vol)
        median = week.get("median")
        if median and make_match_key(name, None):
            REF_INPUTS[name].append(float(median))


def fetch_csfloat_listings() -> dict:
    """{key: Listing} from CSFloat's listings endpoint (lowest per item).
    Captures listing id (for buy-order lookups), tradable, Doppler phase, and
    the platform's predicted price as a reference input.
    Docs: https://docs.csfloat.com/  Auth: `Authorization: <key>`. Cents."""
    if not CSFLOAT_API_KEY:
        return {}

    url = "https://csfloat.com/api/v1/listings"
    headers = {"Authorization": CSFLOAT_API_KEY}
    params = {
        "sort_by": "lowest_price",
        "limit": 50,
        "min_price": int(MIN_ITEM_PRICE_USD * 100),
    }

    store = {}
    cursor = None
    retries_429 = 0
    pages = 0
    while pages < 10:
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
        rows = data.get("data", []) if isinstance(data, dict) else data
        cursor = data.get("cursor") if isinstance(data, dict) else None
        if not rows:
            break
        for row in rows:
            item = row.get("item", {})
            name = item.get("market_hash_name")
            price_cents = row.get("price")
            if not (name and price_cents):
                continue
            reference = row.get("reference") or {}
            ref_cents = reference.get("predicted_price") or reference.get("base_price")
            ingest_listing(
                store, "CSFloat", name, price_cents / 100.0,
                f"https://csfloat.com/item/{row.get('id')}",
                tradable=_csfloat_tradable(item), listing_id=row.get("id"),
                phase=item.get("phase"),
                is_stattrak=item.get("is_stattrak"), is_souvenir=item.get("is_souvenir"),
                ref_input=ref_cents / 100.0 if ref_cents else None,
            )
        pages += 1
        if not cursor:
            break
    return store


def _csfloat_tradable(item: dict) -> Optional[bool]:
    """UNVERIFIED semantics: a future lock-expiry timestamp means locked;
    otherwise fall back to the `tradable` flag."""
    for k in ("tradable_at", "trade_lock_until", "lock_expires_at"):
        ts = item.get(k)
        if isinstance(ts, (int, float)) and ts > time.time():
            return False
    if "tradable" in item:
        return bool(item.get("tradable"))
    return None


def fetch_dmarket_listings() -> dict:
    """{key: Listing} from DMarket market items (lowest offer per title), signed.
    Every observed ask feeds the reference (median of low asks = fallback ref).
    Endpoint: GET /exchange/v1/market/items — UNVERIFIED, prices assumed cents."""
    if not (DMARKET_PUBLIC_KEY and DMARKET_SECRET_KEY):
        return {}

    store = {}
    cursor = None
    pages = 0
    while pages < 10:
        q = (f"/exchange/v1/market/items?gameId={DMARKET_GAME_ID}"
             f"&currency=USD&limit=100&orderBy=price&orderDir=asc")
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
            extra = obj.get("extra") or {}
            url = f"https://dmarket.com/ingame-items/item-list/csgo-skins?title={quote(name)}"
            ingest_listing(store, "DMarket", name, price, url,
                           tradable=_dmarket_tradable(obj), phase=extra.get("phase"),
                           ref_input=price)
        pages += 1
        if not cursor:
            break
    return store


def _dmarket_tradable(obj: dict) -> Optional[bool]:
    """UNVERIFIED. DMarket exposes lock info via `extra`; try common shapes."""
    extra = obj.get("extra") or {}
    for k in ("tradeLockDuration", "tradeLock", "lockDuration"):
        v = extra.get(k)
        if isinstance(v, (int, float)):
            return v <= 0
    if "tradable" in extra:
        return bool(extra.get("tradable"))
    lock = obj.get("lockStatus")
    if isinstance(lock, bool):
        return not lock
    return None


def fetch_csgoroll_listings() -> dict:
    """{key: Listing} from CSGORoll's UNOFFICIAL GraphQL endpoint (the one the
    site's own web client uses). CSGORoll publishes no official API; the owner
    explicitly authorized this read-only scrape. Guardrails: plain request, no
    UA spoofing, no proxies, no retries against blocks — a 4xx/403 means skip.
    Prices are COINS -> converted via CSGOROLL_COIN_USD and marked
    cash_equivalent=False (never a sell venue; coins aren't withdrawable cash).
    ENTIRELY UNVERIFIED — expect to adjust the query/fields on first live run."""
    if not CSGOROLL_ENABLED:
        return {}
    url = "https://api.csgoroll.com/graphql"
    query = {
        "operationName": "TradeList",
        "query": (
            "query TradeList($first: Int, $status: [TradeStatus!]) {"
            " trades(first: $first, status: $status) {"
            " edges { node { id totalValue tradeItems { marketName value } } } } }"
        ),
        "variables": {"first": 100, "status": ["LISTED"]},
    }
    try:
        resp = requests.post(url, json=query, timeout=REQUEST_TIMEOUT)
        if resp.status_code in (401, 403, 429):
            print(f"  CSGORoll returned {resp.status_code} — skipping (no retry/evasion).")
            return {}
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  CSGORoll request failed ({e.__class__.__name__}) — skipping.")
        return {}

    store = {}
    edges = (((payload.get("data") or {}).get("trades") or {}).get("edges")) or []
    for edge in edges:
        node = edge.get("node") or {}
        for it in node.get("tradeItems") or []:
            name = it.get("marketName")
            coins = it.get("value")
            if not (name and isinstance(coins, (int, float))):
                continue
            ingest_listing(
                store, "CSGORoll", name, float(coins) * CSGOROLL_COIN_USD,
                "https://www.csgoroll.com/en/withdraw/csgo",
                tradable=None, cash_equivalent=False,
            )
    return store


# ---------------------------------------------------------------------------
# Sell-side buy-order fetchers
# ---------------------------------------------------------------------------

def fetch_csfloat_buy_orders(listings: dict, candidates: list) -> dict:
    """{key: BuyOrder} — highest UNCONDITIONAL CSFloat buy order per candidate.
    Uses CSFloat's internal buy-order endpoint (NOT in official docs) —
    per-listing, capped at MAX_BUY_ORDER_LOOKUPS. Orders carrying float/attribute
    restrictions are skipped: we can't verify our item satisfies them, and a
    restricted bid our item can't fill is exactly how fake spreads happen."""
    if not (CSFLOAT_API_KEY and CSFLOAT_BUY_ORDERS_ENABLED):
        return {}

    headers = {"Authorization": CSFLOAT_API_KEY}
    orders = {}
    looked_up = 0
    for key in candidates:
        if looked_up >= MAX_BUY_ORDER_LOOKUPS:
            break
        listing = listings.get(key)
        if not listing or not listing.listing_id:
            continue
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
        best = None
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            # Conditional/attribute-restricted order (e.g. float band): skip.
            if r.get("expression") or r.get("min_float") or r.get("max_float"):
                reject("conditional_order", key)
                continue
            price = r.get("price")
            if not isinstance(price, (int, float)):
                continue
            if best is None or price > best[0]:
                qty = r.get("qty")
                best = (price, int(qty) if isinstance(qty, (int, float)) else None)
        if best:
            orders[key] = BuyOrder("CSFloat", key, best[0] / 100.0, listing.url, depth=best[1])
    return orders


def fetch_dmarket_buy_orders(candidates: list) -> dict:
    """{key: BuyOrder} — highest DMarket target per candidate, with `amount` as
    depth. Endpoint: GET /marketplace-api/v1/targets-by-title/<gameId>/<title>
    (signed). UNVERIFIED. NOTE: title lookups can't carry Doppler phase, so
    phase-keyed candidates are looked up by base name; DMarket targets may or
    may not be phase-specific — treat Doppler fills there with extra care."""
    if not (DMARKET_PUBLIC_KEY and DMARKET_SECRET_KEY):
        return {}

    orders = {}
    looked_up = 0
    for key in candidates:
        if looked_up >= MAX_BUY_ORDER_LOOKUPS:
            break
        looked_up += 1
        title = base_name(key)
        data = _dmarket_signed_get(
            f"/marketplace-api/v1/targets-by-title/{DMARKET_GAME_ID}/{quote(title)}")
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
            url = f"https://dmarket.com/ingame-items/item-list/csgo-skins?title={quote(title)}"
            depth = int(best_depth) if isinstance(best_depth, (int, float)) else None
            orders[key] = BuyOrder("DMarket", key, best_price, url, depth=depth)
    return orders


# ---------------------------------------------------------------------------
# DMarket request signing (Ed25519)
# ---------------------------------------------------------------------------

def _dmarket_signed_get(path_with_query: str):
    """Signed GET against DMarket. Returns parsed JSON or None on any failure,
    so one bad source never takes the whole scan down.
    Signs method+path+body+timestamp with Ed25519 (hex secret key); headers:
    X-Api-Key, X-Request-Sign: "dmar ed25519 <hexsig>", X-Sign-Date."""
    try:
        from nacl.encoding import HexEncoder
        from nacl.signing import SigningKey
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
    """DMarket money -> USD float. Cents assumed. Accepts {'USD': '1234'},
    {'amount': '1234'}, or a bare number. UNVERIFIED units."""
    if money is None:
        return None
    raw = money.get("USD", money.get("amount")) if isinstance(money, dict) else money
    try:
        return float(raw) / 100.0 if raw is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Core: pair buy legs against achievable sell quotes
# ---------------------------------------------------------------------------

def find_opportunities(listings_by_platform: dict, orders_by_platform: dict) -> list:
    """listings_by_platform: {platform: {key: Listing}}
       orders_by_platform:   {platform: {key: BuyOrder}}"""
    listings_by_key = defaultdict(dict)
    for plat, d in listings_by_platform.items():
        for key, lst in d.items():
            listings_by_key[key][plat] = lst
    orders_by_key = defaultdict(dict)
    for plat, d in orders_by_platform.items():
        for key, order in d.items():
            orders_by_key[key][plat] = order

    opportunities = []
    for key, buy_side in listings_by_key.items():
        # Liquidity floor: recent sales volume, where the data exists.
        vol = SALES_7D.get(base_name(key))
        if vol is not None and MIN_RECENT_SALES and vol < MIN_RECENT_SALES:
            reject("low_sales", key, f"{vol} sales/7d")
            continue

        ref = reference_price(key)

        # Build achievable sell quotes. INSTANT: surviving top bids.
        quotes = []
        for plat, order in orders_by_key.get(key, {}).items():
            if deviates(order.price, ref):
                reject("outlier_bid", key, f"bid ${order.price:.2f} vs ref ${ref:.2f}")
                continue
            if MIN_BUY_ORDER_DEPTH and order.depth is not None and order.depth < MIN_BUY_ORDER_DEPTH:
                reject("thin_depth", key, f"depth {order.depth}")
                continue
            net = order.price * (1 - BUY_ORDER_FEE_PCT.get(plat, 0.0) / 100.0)
            quotes.append(SellQuote(plat, order.price, net, order.url, True, order.depth))

        # NOT-INSTANT: lowest ask on platforms without a surviving bid. We'd
        # have to match/undercut it and wait — proceeds are an upper bound.
        if ALLOW_NOT_INSTANT:
            instant_plats = {q.platform for q in quotes}
            for plat, lst in buy_side.items():
                if plat in instant_plats or plat not in LISTING_FEE_PCT:
                    continue
                if not lst.cash_equivalent:
                    continue  # coin site: not a cash exit
                if deviates(lst.price, ref):
                    continue  # counted as outlier_listing when it's a buy leg
                net = lst.price * (1 - LISTING_FEE_PCT[plat] / 100.0)
                quotes.append(SellQuote(plat, lst.price, net, lst.url, False))
        if not quotes:
            continue

        for buy_plat, lst in buy_side.items():
            if lst.price < MIN_ITEM_PRICE_USD:
                continue
            if deviates(lst.price, ref):
                reject("outlier_listing", key, f"ask ${lst.price:.2f} vs ref ${ref:.2f}")
                continue
            for q in quotes:
                same = (q.platform == buy_plat)
                if same and not q.instant:
                    continue  # relisting on the same platform is not an opportunity
                spread = (q.net - lst.price) / lst.price * 100 if lst.price > 0 else 0
                if spread < MIN_SPREAD_PCT:
                    continue
                if spread > MAX_PLAUSIBLE_SPREAD_PCT:
                    reject("implausible_spread", key, f"{spread:.0f}%")
                    continue
                if not same and REQUIRE_TRADABLE_NOW:
                    if lst.tradable is False:
                        reject("trade_locked", key)
                        continue
                    if lst.tradable is None and not TREAT_UNKNOWN_TRADABLE_AS_OK:
                        reject("tradable_unknown", key)
                        continue
                opportunities.append(Opportunity(
                    item_key=key,
                    buy_platform=buy_plat,
                    buy_price=lst.price,
                    buy_url=lst.url,
                    sell=q,
                    spread_pct=spread,
                    kind="same-platform" if same else "cross-platform",
                    tradable_now=lst.tradable,
                    reference=ref,
                    coin_note=("" if lst.cash_equivalent else
                               f"buy leg is COIN-priced (converted @ {CSGOROLL_COIN_USD}/coin) — not cash-equivalent"),
                ))

    # Instant same-platform (fastest) first, then instant cross, then
    # not-instant; by spread within each group.
    def sort_key(o):
        group = 0 if (o.sell.instant and o.kind == "same-platform") else (1 if o.sell.instant else 2)
        return (group, -o.spread_pct)
    opportunities.sort(key=sort_key)
    return opportunities


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _tradable_label(o: Opportunity) -> str:
    if o.kind == "same-platform":
        if o.tradable_now is False:
            return "same-platform — but item is trade-LOCKED, delivery delayed"
        return "same-platform (no transfer)"
    if o.tradable_now is True:
        return "tradable now: yes"
    if o.tradable_now is False:
        return "tradable now: NO (locked)"
    return "tradable now: unknown"


def format_opportunity(o: Opportunity) -> str:
    speed = "INSTANT" if o.sell.instant else "NOT INSTANT — needs a buyer"
    tag = "SAME-PLATFORM MISPRICING" if o.kind == "same-platform" else "cross-platform"
    depth = f", depth {o.sell.depth}" if o.sell.depth is not None else ""
    mech = (f"fill top buy order ${o.sell.gross:.2f}{depth}" if o.sell.instant
            else f"undercut lowest ask ${o.sell.gross:.2f} and wait")
    ref = f" | ref ${o.reference:.2f}" if o.reference else ""
    warn = "⚠️ " if (o.tradable_now is False or
                     (o.kind == "cross-platform" and o.tradable_now is None)) else ""
    lines = [
        f"[{tag} | {speed}] {base_name(o.item_key)}"
        + (f" ({o.item_key.split(' · ')[1]})" if " · " in o.item_key else "")
        + f" — {o.spread_pct:.1f}% net{ref}",
        f"Buy:  {o.buy_platform} @ ${o.buy_price:.2f} — {o.buy_url}",
        f"Sell: {o.sell.platform} — {mech} → net ${o.sell.net:.2f} after fee — {o.sell.url}",
        f"{warn}{_tradable_label(o)}",
    ]
    if o.coin_note:
        lines.append(f"⚠️ {o.coin_note}")
    return "\n".join(lines)


def print_rejection_summary(n_accepted: int):
    print("\n--- Filter summary ---")
    print(f"accepted: {n_accepted}")
    for reason in REJECT_REASONS:
        n = REJECTIONS.get(reason, 0)
        samples = f"  e.g. {'; '.join(REJECT_SAMPLES[reason])}" if n else ""
        print(f"rejected {reason:<20} {n}{samples}")


def send_discord_alert(opportunities: list):
    if not opportunities:
        return
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set — console only.")
        return
    lines = ["**CS2 Arbitrage — achievable-exit opportunities:**\n"]
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
    """Fetch listings, sales references, and buy orders from every enabled
    source. Sources are isolated: one failure skips that source only."""
    listings_by_platform = {}
    for label, fn in (
        ("Skinport", fetch_skinport_listings),
        ("CSFloat", fetch_csfloat_listings),
        ("DMarket", fetch_dmarket_listings),
        ("CSGORoll", fetch_csgoroll_listings),
    ):
        try:
            d = fn()
            if d:
                listings_by_platform[label] = d
                print(f"  {label} listings: {len(d)}")
        except requests.RequestException as e:
            print(f"  {label} listings failed ({e.__class__.__name__}) — skipping.")

    try:
        fetch_skinport_sales()
        print(f"  Skinport sales history: {len(SALES_7D)} items")
    except requests.RequestException as e:
        print(f"  Skinport sales history failed ({e.__class__.__name__}) — references degrade.")

    candidates = sorted({
        key
        for d in listings_by_platform.values()
        for key, lst in d.items()
        if lst.price >= MIN_ITEM_PRICE_USD
    })

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
    REJECTIONS.clear()
    REJECT_SAMPLES.clear()
    REF_INPUTS.clear()
    SALES_7D.clear()

    if DRY_RUN:
        print("DRY RUN — using built-in fixtures, no network calls.\n")
        listings_by_platform, orders_by_platform = build_mock_data()
    else:
        print("Fetching listings (buy side), sales references, buy orders (sell side)...")
        listings_by_platform, orders_by_platform = gather()

    opportunities = find_opportunities(listings_by_platform, orders_by_platform)

    if opportunities:
        same = sum(1 for o in opportunities if o.kind == "same-platform")
        instant = sum(1 for o in opportunities if o.sell.instant)
        print(f"\nFound {len(opportunities)} opportunities "
              f"({same} same-platform, {instant} instant):\n")
        for o in opportunities[:15]:
            print(format_opportunity(o) + "\n")
    else:
        print(f"\nNo opportunities in the {MIN_SPREAD_PCT}–{MAX_PLAUSIBLE_SPREAD_PCT}% window this run.")

    print_rejection_summary(len(opportunities))
    send_discord_alert(opportunities)


# ---------------------------------------------------------------------------
# Fixtures for the offline dry run (SCANNER_DRY_RUN=1)
# ---------------------------------------------------------------------------

def build_mock_data() -> tuple:
    """Synthetic data exercising every accept path AND every rejection counter.
    Run with MIN_SPREAD_PCT=8 to see all accepted cases. Fixtures go through
    the same ingest_listing() path as real fetchers, so condition checks fire."""
    SALES_7D.update({
        "AK-47 | Redline (Field-Tested)": 40,
        "Glock-18 | Fade (Factory New)": 12,
        "AWP | Asiimov (Field-Tested)": 25,
        "M4A4 | Howl (Minimal Wear)": 5,
        "P250 | Sand Dune (Field-Tested)": 30,
        "MP9 | Storm (Minimal Wear)": 22,
        "Tec-9 | Nuclear Threat (Minimal Wear)": 10,
        "Five-SeveN | Case Hardened (Field-Tested)": 15,
        "SSG 08 | Abyss (Field-Tested)": 1,          # -> low_sales
        "AK-47 | Asiimov (Field-Tested)": 33,
    })

    sp, cf, dm, roll = {}, {}, {}, {}

    # A. Same-platform mispricing (CSFloat listing under CSFloat top bid).
    ingest_listing(cf, "CSFloat", "AK-47 | Redline (Field-Tested)", 18.50,
                   "https://csfloat.com/item/1", tradable=True, listing_id="1", ref_input=20.0)
    #    Skinport listing, lock unknown -> its cross pair gets tradable_unknown.
    ingest_listing(sp, "Skinport", "AK-47 | Redline (Field-Tested)", 20.00,
                   "https://skinport.com/x", tradable=None)

    # B. Valid cross-platform: CSFloat (tradable) -> DMarket bid.
    ingest_listing(cf, "CSFloat", "Glock-18 | Fade (Factory New)", 400.00,
                   "https://csfloat.com/item/2", tradable=True, listing_id="2", ref_input=440.0)

    # C. Not-instant fallback: DMarket buy -> undercut Skinport's lowest ask (no bids anywhere).
    ingest_listing(dm, "DMarket", "AWP | Asiimov (Field-Tested)", 50.00,
                   "https://dmarket.com/z", tradable=True, ref_input=55.0)
    ingest_listing(sp, "Skinport", "AWP | Asiimov (Field-Tested)", 60.00,
                   "https://skinport.com/y", tradable=None)

    # D. Trade-locked: same-platform allowed (labeled), cross rejected.
    ingest_listing(cf, "CSFloat", "M4A4 | Howl (Minimal Wear)", 3000.00,
                   "https://csfloat.com/item/3", tradable=False, listing_id="3", ref_input=3500.0)

    # F. Outlier listing: ask 60% below reference -> rejected before math.
    ingest_listing(cf, "CSFloat", "P250 | Sand Dune (Field-Tested)", 40.00,
                   "https://csfloat.com/item/4", tradable=True, listing_id="4", ref_input=100.0)

    # G. Outlier bid: bid 50% above reference -> rejected.
    ingest_listing(cf, "CSFloat", "MP9 | Storm (Minimal Wear)", 28.00,
                   "https://csfloat.com/item/5", tradable=True, listing_id="5", ref_input=30.0)

    # H. Implausible spread: both legs within deviation but spread ~47% -> capped.
    ingest_listing(dm, "DMarket", "Tec-9 | Nuclear Threat (Minimal Wear)", 80.00,
                   "https://dmarket.com/tec9", tradable=True, ref_input=100.0)

    # I. Thin depth: qty-1 top bid -> rejected as bait.
    ingest_listing(cf, "CSFloat", "Five-SeveN | Case Hardened (Field-Tested)", 45.00,
                   "https://csfloat.com/item/6", tradable=True, listing_id="6", ref_input=50.0)

    # J. Low sales: 1 sale/7d < MIN_RECENT_SALES.
    ingest_listing(cf, "CSFloat", "SSG 08 | Abyss (Field-Tested)", 20.00,
                   "https://csfloat.com/item/7", tradable=True, listing_id="7", ref_input=21.0)

    # K. Doppler phase handling: unknown phase dropped; known phases match.
    ingest_listing(sp, "Skinport", "★ Karambit | Doppler (Factory New)", 850.00,
                   "https://skinport.com/k", tradable=None)          # -> phase_unknown
    ingest_listing(cf, "CSFloat", "★ Karambit | Doppler (Factory New)", 900.00,
                   "https://csfloat.com/item/8", tradable=True, listing_id="8",
                   phase="Phase 2", ref_input=1000.0)

    # L. Condition mismatch: StatTrak flag contradicts the name -> dropped.
    ingest_listing(cf, "CSFloat", "USP-S | Kill Confirmed (Minimal Wear)", 120.00,
                   "https://csfloat.com/item/9", tradable=True, listing_id="9",
                   is_stattrak=True, ref_input=125.0)

    # M. CSGORoll coin-priced buy leg (100 coins @ 0.66 = $66), labeled non-cash.
    ingest_listing(roll, "CSGORoll", "AK-47 | Asiimov (Field-Tested)", 100 * CSGOROLL_COIN_USD,
                   "https://www.csgoroll.com/en/withdraw/csgo", tradable=True,
                   cash_equivalent=False, ref_input=78.0)

    listings_by_platform = {"Skinport": sp, "CSFloat": cf, "DMarket": dm, "CSGORoll": roll}

    orders_by_platform = {
        "CSFloat": {
            "AK-47 | Redline (Field-Tested)":
                BuyOrder("CSFloat", "AK-47 | Redline (Field-Tested)", 23.00, "https://csfloat.com/item/1", depth=5),
            "M4A4 | Howl (Minimal Wear)":
                BuyOrder("CSFloat", "M4A4 | Howl (Minimal Wear)", 3800.00, "https://csfloat.com/howl", depth=2),
            "Tec-9 | Nuclear Threat (Minimal Wear)":
                BuyOrder("CSFloat", "Tec-9 | Nuclear Threat (Minimal Wear)", 120.00, "https://csfloat.com/tec9", depth=4),
        },
        "DMarket": {
            "Glock-18 | Fade (Factory New)":
                BuyOrder("DMarket", "Glock-18 | Fade (Factory New)", 470.00, "https://dmarket.com/fade", depth=3),
            "M4A4 | Howl (Minimal Wear)":
                BuyOrder("DMarket", "M4A4 | Howl (Minimal Wear)", 3800.00, "https://dmarket.com/howl", depth=3),
            "MP9 | Storm (Minimal Wear)":
                BuyOrder("DMarket", "MP9 | Storm (Minimal Wear)", 45.00, "https://dmarket.com/storm", depth=5),
            "Five-SeveN | Case Hardened (Field-Tested)":
                BuyOrder("DMarket", "Five-SeveN | Case Hardened (Field-Tested)", 55.00, "https://dmarket.com/cs", depth=1),
            "SSG 08 | Abyss (Field-Tested)":
                BuyOrder("DMarket", "SSG 08 | Abyss (Field-Tested)", 25.00, "https://dmarket.com/abyss", depth=4),
            "★ Karambit | Doppler (Factory New) · Phase 2":
                BuyOrder("DMarket", "★ Karambit | Doppler (Factory New) · Phase 2", 1050.00, "https://dmarket.com/kara", depth=2),
            "AK-47 | Asiimov (Field-Tested)":
                BuyOrder("DMarket", "AK-47 | Asiimov (Field-Tested)", 80.00, "https://dmarket.com/asiimov-ak", depth=3),
            "P250 | Sand Dune (Field-Tested)":
                BuyOrder("DMarket", "P250 | Sand Dune (Field-Tested)", 95.00, "https://dmarket.com/sd", depth=5),
        },
    }
    return listings_by_platform, orders_by_platform


if __name__ == "__main__":
    main()
