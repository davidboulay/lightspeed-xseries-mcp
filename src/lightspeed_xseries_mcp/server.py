"""Lightspeed Retail (X-Series) MCP server, read-only.

One process serves one retailer account (LIGHTSPEED_DOMAIN_PREFIX, e.g. mystore
for mystore.retail.lightspeed.app) with that account's personal token
(LIGHTSPEED_TOKEN). Personal tokens have no
scopes, so read-only is enforced here: every tool issues GET requests, except
the two inventory list endpoints, which are reads that take their filters as a
POST body (/inventory, /inventory_levels). There are no write tools.

API: date-based version LIGHTSPEED_API_VERSION (default 2026-10), base URL
https://<prefix>.retail.lightspeed.app/api/<version>. Docs:
https://x-series-api.lightspeedhq.com/llms.txt
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

PREFIX = os.environ["LIGHTSPEED_DOMAIN_PREFIX"]
TOKEN = os.environ["LIGHTSPEED_TOKEN"]
VERSION = os.environ.get("LIGHTSPEED_API_VERSION", "2026-10")
BASE = f"https://{PREFIX}.retail.lightspeed.app/api/{VERSION}"

mcp = MCPServer(
    f"lightspeed-{PREFIX}",
    instructions=(
        f"Read-only access to the Lightspeed Retail (X-Series) account '{PREFIX}'. "
        "Dates you pass as YYYY-MM-DD are local store dates (retailer time zone). "
        "Use sales_summary for revenue questions, search_sales to find sales, "
        "search_products then get_inventory for stock. Amounts are in the retailer currency "
        "(see get_retailer)."
    ),
)
RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
tool = lambda: mcp.tool(annotations=RO)  # noqa: E731

# Paths api_get refuses: integration plumbing that can carry credentials or
# partner data, and nothing a retail question needs.
DENY = re.compile(r"^/(webhooks|partner|channel_requests|security_events)(/|$)")


class LightspeedError(Exception):
    pass


def _request(method, path, params=None, body=None, retry=True):
    query = urllib.parse.urlencode(
        [(k, v) for k, vals in (params or {}).items() if vals is not None
         for v in (vals if isinstance(vals, (list, tuple)) else [vals])],
    )
    url = f"{BASE}{path}" + (f"?{query}" if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json",
        "User-Agent": "lightspeed-xseries-mcp/1.0",
        **({"Content-Type": "application/json"} if data else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 429 and retry:
            time.sleep(min(_retry_after(e.headers.get("Retry-After")), 20))
            return _request(method, path, params, body, retry=False)
        detail = e.read().decode(errors="replace")[:500]
        raise LightspeedError(f"Lightspeed {e.code} on {method} {path}: {detail}") from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"text": raw.decode(errors="replace")}


def _retry_after(value):
    if not value:
        return 5
    if value.isdigit():
        return int(value)
    try:
        when = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
        return max(1, (when - datetime.now(timezone.utc)).total_seconds())
    except ValueError:
        return 5


def get(path, **params):
    return _request("GET", path, params)


def _data(resp):
    return resp.get("data", resp) if isinstance(resp, dict) else resp


# --- small per-process caches (reference data changes rarely) -----------------

_cache = {}


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.monotonic(), value)
    return value


def _paged(path, page_size=1000, limit=20000, **params):
    """Walk a version-cursor collection (after = previous page's version.max)."""
    out, after = [], 0
    while len(out) < limit:
        resp = get(path, after=after or None, page_size=page_size, **params)
        items = _data(resp) or []
        if not items:
            break
        out.extend(items)
        nxt = (resp.get("version") or {}).get("max") if isinstance(resp, dict) else None
        if not nxt or nxt == after:
            break
        after = nxt
    return out[:limit]


def _retailer():
    return _cached("retailer", 3600, lambda: _data(get("/retailer")))


def _tz():
    r = _retailer() or {}
    name = r.get("time_zone") or r.get("timezone") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _outlets():
    return _cached("outlets", 3600, lambda: {o["id"]: o.get("name") for o in _paged("/outlets")})


def _users():
    return _cached("users", 3600, lambda: {u["id"]: u.get("display_name") or u.get("username") for u in _paged("/users")})


def _catalog():
    """Every product with its family name, for name search (refreshed every 15 min)."""
    def load():
        products, families, after = [], {}, 0
        while True:
            resp = get("/products", after=after or None, page_size=1000, **{"includes[]": "families"})
            items = _data(resp) or []
            for f in ((resp.get("includes") or {}).get("product_families") or []):
                families[f["id"]] = f
            if not items:
                break
            products.extend(items)
            nxt = (resp.get("version") or {}).get("max")
            if not nxt or nxt == after:
                break
            after = nxt
        rows = []
        for p in products:
            if p.get("deleted_at"):
                continue
            fam = families.get(p.get("family_id")) or {}
            name = fam.get("name") or p.get("name") or ""
            variant = p.get("variant_name") or ""
            prices = p.get("prices") or {}
            rows.append({
                "product_id": p.get("id"),
                "family_id": p.get("family_id"),
                "name": name,
                "variant_name": variant,
                "sku": p.get("sku"),
                "codes": [c.get("code") for c in (p.get("codes") or []) if c.get("code")],
                "price_including_tax": prices.get("price_including_tax"),
                "price_excluding_tax": prices.get("price_excluding_tax"),
                "active": p.get("active"),
                "_haystack": " ".join([name, variant, p.get("sku") or ""] +
                                      [c.get("code") or "" for c in (p.get("codes") or [])]).lower(),
            })
        return rows
    return _cached("catalog", 900, load)


def _label(row):
    """'Weekender / Navy / Large [SKU]': variant_name already carries the product name."""
    text = row.get("variant_name") or row.get("name") or row.get("product_id") or "?"
    return text + (f" [{row['sku']}]" if row.get("sku") else "")


def _local_range(date_from, date_to):
    """YYYY-MM-DD local dates (inclusive) -> UTC ISO bounds for /search."""
    tz = _tz()
    d0 = date.fromisoformat(date_from)
    d1 = date.fromisoformat(date_to) if date_to else d0
    start = datetime.combine(d0, datetime.min.time(), tz).astimezone(timezone.utc)
    end = datetime.combine(d1 + timedelta(days=1), datetime.min.time(), tz).astimezone(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def _today():
    return datetime.now(_tz()).date().isoformat()


# --- sale field access (search returns the 2.x sale shape; /sales the date-based one) ---

def _sale_outlet(s):
    return s.get("outlet_id") or (s.get("source") or {}).get("outlet_id")


def _sale_user(s):
    return s.get("user_id") or ((s.get("source") or {}).get("author") or {}).get("id")


def _sale_date(s):
    return s.get("sale_date") or s.get("date") or s.get("created_at")


def _sale_totals(s):
    t = s.get("totals") or {}
    excl = t.get("price", s.get("total_price"))
    tax = t.get("tax", s.get("total_tax"))
    incl = t.get("price_incl_tax")
    if incl is None and excl is not None:
        incl = excl + (tax or 0)
    return float(excl or 0), float(tax or 0), float(incl or 0)


def _sale_state(s):
    return s.get("state") or s.get("status")


def _line_amounts(li):
    """(product_id, quantity, total excl tax, total tax) of a sale line, either shape."""
    pid = li.get("product_id") or (li.get("product") or {}).get("id")
    qty = float(li.get("quantity") or 0)
    excl = li.get("price_total")
    if excl is None:
        excl = (li.get("pricing") or {}).get("total_price")
    if excl is None:
        excl = float(li.get("price") or 0) * qty
    tax = li.get("tax_total")
    if tax is None:
        t = li.get("tax")
        tax = t.get("total", 0) if isinstance(t, dict) else float(t or 0) * qty
    return pid, qty, float(excl or 0), float(tax or 0)


def _local_day(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_tz()).date().isoformat()


def _compact_sale(s):
    excl, tax, incl = _sale_totals(s)
    return {
        "id": s.get("id"),
        "invoice_number": s.get("invoice_number"),
        "date": _sale_date(s),
        "local_day": _local_day(_sale_date(s)),
        "state": _sale_state(s),
        "outlet": _outlets().get(_sale_outlet(s), _sale_outlet(s)),
        "user": _users().get(_sale_user(s), _sale_user(s)),
        "customer_id": s.get("customer_id"),
        "total_excl_tax": round(excl, 2),
        "tax": round(tax, 2),
        "total_incl_tax": round(incl, 2),
        "line_items": len(s.get("line_items") or s.get("register_sale_products") or []),
        "note": s.get("note") or None,
    }


def _search_sales(date_from, date_to, states, outlet_id=None, extra=None, limit=5000):
    lo, hi = _local_range(date_from, date_to)
    out, offset = [], 0
    while len(out) < limit:
        params = {"type": "sales", "date_from": lo, "date_to": hi, "outlet_id": outlet_id,
                  "page_size": 1000, "offset": offset, "order_by": "date", "order_direction": "asc",
                  **(extra or {})}
        if states:
            params["state"] = states
        items = _data(get("/search", **params)) or []
        out.extend(items)
        if len(items) < 1000:
            break
        offset += 1000
    return out[:limit]


# ============================================================================
# Account and reference data
# ============================================================================

@tool()
def get_retailer() -> dict | list:
    """Retailer account details: name, currency, time zone, settings. Call this
    first if you need the currency or local time zone."""
    return _retailer()


@tool()
def list_outlets() -> dict | list:
    """All outlets (stores/warehouses) with id, name, address and time zone."""
    return _paged("/outlets")


@tool()
def list_registers() -> dict | list:
    """All registers (tills) with their outlet and open/closed state."""
    return _paged("/registers")


@tool()
def list_users() -> dict | list:
    """Staff users (id, name, account type, outlets). Use ids with get_user_sale_totals
    or search_sales(user_id=...)."""
    return [{k: u.get(k) for k in ("id", "username", "display_name", "email", "account_type",
                                   "restricted_outlet_ids", "is_primary_user", "deleted_at")}
            for u in _paged("/users")]


@tool()
def list_payment_types() -> dict | list:
    """Payment types configured (cash, card, gift card, store credit, ...)."""
    return _paged("/payment_types")


@tool()
def list_taxes() -> dict | list:
    """Sales taxes configured, with rates."""
    return _paged("/taxes")


@tool()
def list_reference(kind: str) -> dict | list:
    """List a reference collection. kind is one of: brands, suppliers, tags,
    product_categories, product_types, variant_attributes, customer_groups,
    price_books, outlet_taxes, button_layouts."""
    kinds = {"brands", "suppliers", "tags", "product_categories", "product_types",
             "variant_attributes", "customer_groups", "price_books", "outlet_taxes", "button_layouts"}
    if kind not in kinds:
        raise LightspeedError(f"kind must be one of {sorted(kinds)}")
    if kind == "variant_attributes":
        return _data(get("/variant_attributes"))
    if kind == "product_categories":
        resp = _data(get("/product_categories", page_size=1000))
        if isinstance(resp, dict):
            resp = resp.get("data", resp)
        return resp.get("categories", resp) if isinstance(resp, dict) else resp
    return _paged(f"/{kind}")


# ============================================================================
# Products and stock
# ============================================================================

@tool()
def search_products(query: str, limit: int = 25, include_inactive: bool = False) -> dict | list:
    """Find products by name, variant name, SKU or barcode (case-insensitive; every
    word must match). Returns product_id, name, variant, SKU and retail prices.
    Follow with get_inventory(product_id) for stock or get_product for full detail."""
    words = query.lower().split()
    rows = [r for r in _catalog() if all(w in r["_haystack"] for w in words)]
    if not include_inactive:
        rows = [r for r in rows if (r.get("active") or {}).get("in_store", True) is not False]
    return {
        "matches": len(rows),
        "products": [{k: v for k, v in r.items() if k != "_haystack"} for r in rows[:limit]],
    }


@tool()
def get_product(product_id: str | None = None, sku: str | None = None) -> dict | list:
    """One product with its family, brand, category, supplier, tags and variant
    attributes. Pass product_id, or sku to look it up by SKU."""
    inc = ["families", "brands", "categories", "suppliers", "tags", "variant_attributes", "composite_components"]
    if sku:
        resp = get("/products", sku=sku, **{"includes[]": inc})
        return resp
    if not product_id:
        raise LightspeedError("pass product_id or sku")
    return get(f"/products/{product_id}", **{"includes[]": inc})


@tool()
def get_product_family(product_id: str, full: bool = False) -> dict | list:
    """The variant family a product belongs to (every colour/size of one item):
    family details, brand/category/tag names and one compact line per variant (id,
    variant name, SKU, retail price, active). full=True returns the raw API object
    (can be very large)."""
    resp = get(f"/products/{product_id}/family",
               **{"includes[]": ["brands", "categories", "suppliers", "tags", "variant_attributes"]})
    if full:
        return resp
    fam = dict(_data(resp) or {})
    products = fam.pop("products", None) or []
    fam["images"] = [i.get("url") for i in (fam.get("images") or [])][:5]
    fam["variants"] = [{
        "product_id": v.get("id"),
        "variant_name": v.get("variant_name"),
        "sku": v.get("sku"),
        "price_including_tax": (v.get("prices") or {}).get("price_including_tax"),
        "active": v.get("active"),
    } for v in products if not v.get("deleted_at")]
    fam["variant_count"] = len(fam["variants"])
    inc = resp.get("includes") or {} if isinstance(resp, dict) else {}
    fam["names"] = {k: [x.get("name") for x in v] for k, v in inc.items() if isinstance(v, list)}
    return fam


@tool()
def list_products(after: int = 0, page_size: int = 100) -> dict | list:
    """Raw product collection page (version cursor). Pass the previous response's
    version.max as `after` for the next page. Prefer search_products to find items."""
    return get("/products", after=after or None, page_size=page_size, **{"includes[]": "families"})


@tool()
def get_inventory(product_id: str, include_variants: bool = False, outlet: str | None = None,
                  hide_zero: bool = True) -> dict | list:
    """Stock on hand per outlet for one product (variant), with reorder point/amount
    and average cost. include_variants=True covers its whole family (every colour and
    size; can be hundreds of rows). outlet filters by outlet name (substring).
    hide_zero drops rows with nothing on hand."""
    catalog = _catalog()
    names = {r["product_id"]: r for r in catalog}
    outlets = _outlets()
    me = names.get(product_id) or {}
    if include_variants and me.get("family_id"):
        family = [r["product_id"] for r in catalog if r["family_id"] == me["family_id"]]
        resp = _request("POST", "/inventory_levels", body={"product_ids": family[:1000], "size": 10000})
        rows = _data(resp) or []
        out = [{
            "product_id": r.get("product_id"),
            "label": _label(names.get(r.get("product_id")) or {"name": r.get("name")}),
            "outlet": outlets.get(r.get("location_id"), r.get("location_id")),
            "on_hand": r.get("current_inventory_level"),
            "to_procure": r.get("quantity_to_procure"),
            "average_cost": r.get("average_cost"),
        } for r in rows]
        f = _stock_filter(out, outlet, hide_zero)
        by_variant = {}
        for r in f["stock"]:
            v = by_variant.setdefault(r["label"], {"variant": r["label"], "product_id": r["product_id"],
                                                   "total_on_hand": 0, "by_outlet": {}})
            v["total_on_hand"] += r.get("on_hand") or 0
            v["by_outlet"][str(r["outlet"])] = r.get("on_hand")
        return {"variants": len(by_variant), "total_on_hand": f["total_on_hand"],
                "stock": sorted(by_variant.values(), key=lambda v: -v["total_on_hand"])}
    rows = _data(_request("POST", "/inventory", body={"product_id": product_id, "size": 5000})) or []
    out = [{
        "product_id": r.get("product_id"),
        "label": _label(names.get(r.get("product_id")) or {}),
        "outlet": outlets.get(r.get("outlet_id"), r.get("outlet_id")),
        "on_hand": r.get("current_inventory_level"),
        "reorder_point": r.get("reorder_point"),
        "reorder_amount": r.get("reorder_amount"),
        "average_cost": r.get("average_cost"),
    } for r in rows if not r.get("deleted_at")]
    return _stock_filter(out, outlet, hide_zero)


def _stock_filter(rows, outlet, hide_zero):
    if outlet:
        rows = [r for r in rows if outlet.lower() in str(r["outlet"]).lower()]
    if hide_zero:
        rows = [r for r in rows if (r.get("on_hand") or 0) != 0]
    return {"rows": len(rows), "total_on_hand": sum(r.get("on_hand") or 0 for r in rows),
            "stock": sorted(rows, key=lambda r: (r["label"], str(r["outlet"])))}


@tool()
def list_inventory_levels(outlet_ids: list[str] | None = None, product_ids: list[str] | None = None,
                          root_product_ids: list[str] | None = None, group_variants: bool = False,
                          include_inactive: bool = False, offset: int = 0, size: int = 500) -> dict | list:
    """Inventory levels across the catalog, optionally filtered by outlets and products
    (on hand, available, allocated, on order where provided). Page with offset/size
    (size max 10000). Use for 'what's in stock at outlet X' or low-stock reviews."""
    body = {"location_ids": outlet_ids, "product_ids": product_ids, "root_product_ids": root_product_ids,
            "group_variants": group_variants, "include_inactive": include_inactive,
            "offset": offset, "size": size}
    resp = _request("POST", "/inventory_levels", body={k: v for k, v in body.items() if v is not None})
    outlets = _outlets()
    for r in (_data(resp) or []):
        r["outlet"] = outlets.get(r.get("location_id"), r.get("location_id"))
    return resp


@tool()
def list_stock_adjustments(after: int = 0, page_size: int = 100) -> dict | list:
    """Stock adjustments (manual corrections, damages, ...), version-cursor paged."""
    return get("/stock_adjustments", after=after or None, page_size=page_size)


@tool()
def list_consignments(type: str | None = None, status: str | None = None, outlet_id: str | None = None,
                      after: int = 0, page_size: int = 100) -> dict | list:
    """Stock orders/transfers/returns/stocktakes. type: SUPPLIER, OUTLET (transfer),
    RETURN or STOCKTAKE. status e.g. OPEN, SENT, DISPATCHED, RECEIVED, CANCELLED,
    STOCKTAKE_IN_PROGRESS. Version-cursor paged."""
    return get("/consignments", type=type, status=status, outlet_id=outlet_id,
               after=after or None, page_size=page_size)


@tool()
def get_consignment(consignment_id: str, include_products: bool = True) -> dict | list:
    """One consignment with its totals and (by default) its product lines."""
    out = {"consignment": _data(get(f"/consignments/{consignment_id}"))}
    try:
        out["totals"] = _data(get(f"/consignments/{consignment_id}/totals"))
    except LightspeedError as e:
        out["totals"] = str(e)
    if include_products:
        out["products"] = _paged(f"/consignments/{consignment_id}/products", page_size=1000)
    return out


@tool()
def list_purchase_orders(after: int = 0, page_size: int = 100) -> dict | list:
    """Purchase orders (version-cursor paged)."""
    return get("/purchase_orders", after=after or None, page_size=page_size)


@tool()
def get_purchase_order(purchase_order_id: str, include_lines: bool = True) -> dict | list:
    """One purchase order with its line items and deliveries."""
    out = {"purchase_order": _data(get(f"/purchase_orders/{purchase_order_id}"))}
    if include_lines:
        out["line_items"] = _data(get(f"/purchase_orders/{purchase_order_id}/line_items"))
        out["deliveries"] = _data(get(f"/purchase_orders/{purchase_order_id}/deliveries"))
    return out


# ============================================================================
# Sales
# ============================================================================

@tool()
def search_sales(date_from: str | None = None, date_to: str | None = None, outlet_id: str | None = None,
                 states: list[str] | None = None, customer_id: str | None = None,
                 invoice_number: str | None = None, user_id: str | None = None,
                 product_id: str | None = None, register_id: str | None = None,
                 limit: int = 100, compact: bool = True) -> dict | list:
    """Find sales. date_from/date_to are local store dates YYYY-MM-DD, inclusive
    (default: today). states: closed, pending (on account / layby), parked, voided
    (default: all). Compact rows by default; set compact=False for full line items,
    or call get_sale(id)."""
    extra = {"customer_id": customer_id, "invoice_number": invoice_number, "user_id": user_id,
             "product_id": product_id, "register_id": register_id}
    sales = _search_sales(date_from or _today(), date_to, states, outlet_id,
                          {k: v for k, v in extra.items() if v}, limit=limit)
    return {"count": len(sales), "sales": [_compact_sale(s) for s in sales] if compact else sales}


@tool()
def get_sale(sale_id: str) -> dict | list:
    """One sale in full: line items, payments, taxes, customer, totals."""
    return _data(get(f"/sales/{sale_id}"))


@tool()
def sales_summary(date_from: str | None = None, date_to: str | None = None,
                  group_by: str = "outlet", outlet_id: str | None = None,
                  states: list[str] | None = None, top: int = 50) -> dict | list:
    """Revenue totals for a local date range (YYYY-MM-DD, inclusive; default today).
    group_by: outlet, day, user, product, or none. Counts closed sales by default
    (pass states to change). Returns sales count, totals excl/incl tax, tax, units
    (product grouping) and average sale value. Returns are included as negatives.
    Capped at 20,000 sales per call: narrow the range if `truncated` is true."""
    if group_by not in ("outlet", "day", "user", "product", "none"):
        raise LightspeedError("group_by must be outlet, day, user, product or none")
    d0 = date_from or _today()
    sales = _search_sales(d0, date_to, states or ["closed"], outlet_id, limit=20000)
    outlets, users, names = _outlets(), _users(), None
    groups = defaultdict(lambda: {"sales": 0, "total_excl_tax": 0.0, "tax": 0.0, "total_incl_tax": 0.0, "units": 0.0})
    grand = {"sales": 0, "total_excl_tax": 0.0, "tax": 0.0, "total_incl_tax": 0.0}
    for s in sales:
        excl, tax, incl = _sale_totals(s)
        grand["sales"] += 1
        grand["total_excl_tax"] += excl
        grand["tax"] += tax
        grand["total_incl_tax"] += incl
        if group_by == "product":
            if names is None:
                names = {r["product_id"]: r for r in _catalog()}
            for li in s.get("line_items") or s.get("register_sale_products") or []:
                pid, qty, p_excl, p_tax = _line_amounts(li)
                key = _label(names.get(pid) or {"product_id": pid})
                g = groups[key]
                g["sales"] += 1
                g["units"] += qty
                g["total_excl_tax"] += p_excl
                g["tax"] += p_tax
                g["total_incl_tax"] += p_excl + p_tax
            continue
        if group_by == "none":
            continue
        if group_by == "outlet":
            key = outlets.get(_sale_outlet(s), _sale_outlet(s))
        elif group_by == "day":
            key = _local_day(_sale_date(s))
        else:
            key = users.get(_sale_user(s), _sale_user(s))
        g = groups[key]
        g["sales"] += 1
        g["total_excl_tax"] += excl
        g["tax"] += tax
        g["total_incl_tax"] += incl

    def fmt(g):
        out = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in g.items()}
        if group_by != "product":
            out.pop("units", None)
            out["avg_sale_incl_tax"] = round(g["total_incl_tax"] / g["sales"], 2) if g["sales"] else 0
        return out

    ordered = sorted(groups.items(), key=(lambda kv: kv[0] or "") if group_by == "day"
                     else (lambda kv: -kv[1]["total_incl_tax"]))
    return {
        "account": PREFIX,
        "currency": ((_retailer() or {}).get("currency") or {}).get("code"),
        "time_zone": str(_tz()),
        "date_from": d0,
        "date_to": date_to or d0,
        "states": states or ["closed"],
        "truncated": len(sales) >= 20000,
        "total": fmt(grand),
        "groups": [{"key": k, **fmt(v)} for k, v in ordered[:top]] if group_by != "none" else [],
    }


@tool()
def get_user_sale_totals(user_id: str) -> dict | list:
    """A staff user's sales totals (as reported by Lightspeed)."""
    return _data(get(f"/users/{user_id}/sale_totals"))


@tool()
def get_register_payments_summary(register_id: str) -> dict | list:
    """Payment totals by payment type for a register's current/last session."""
    return _data(get(f"/registers/{register_id}/payments_summary"))


@tool()
def list_quotes(after: int = 0, limit: int = 100) -> dict | list:
    """Quotes (sales:read), cursor paged with `after`."""
    return get("/quotes", after=after or None, limit=limit)


@tool()
def list_fulfillments(outlet_id: str | None = None, sale_id: str | None = None,
                      fulfillment_type: str | None = None, fulfillment_state: str | None = None,
                      page_number: int = 1, page_size: int = 100) -> dict | list:
    """Fulfillments (pickups/deliveries/shipping) with state, optionally by outlet or sale."""
    return get("/fulfillments", outlet_id=outlet_id, sale_id=sale_id, fulfillment_type=fulfillment_type,
               fulfillment_state=fulfillment_state, page_number=page_number, page_size=page_size)


# ============================================================================
# Customers, loyalty, gift cards, promotions, price books
# ============================================================================

@tool()
def search_customers(email: str | None = None, first_name: str | None = None, last_name: str | None = None,
                     company_name: str | None = None, mobile: str | None = None, phone: str | None = None,
                     customer_code: str | None = None, limit: int = 50) -> dict | list:
    """Find customers by email, name, company, phone or customer code."""
    params = {"email": email, "first_name": first_name, "last_name": last_name, "company_name": company_name,
              "mobile": mobile, "phone": phone, "customer_code": customer_code}
    params = {k: v for k, v in params.items() if v}
    if not params:
        raise LightspeedError("pass at least one search field")
    return get("/search", type="customers", page_size=limit, **params)


@tool()
def get_customer(customer_id: str, include_store_credit: bool = True) -> dict | list:
    """One customer (contact details, group, loyalty balance, year-to-date spend) with
    addresses and, by default, store credit balance."""
    out = {"customer": _data(get(f"/customers/{customer_id}"))}
    try:
        out["addresses"] = _data(get(f"/customers/{customer_id}/addresses"))
    except LightspeedError as e:
        out["addresses"] = str(e)
    if include_store_credit:
        try:
            out["store_credit"] = _data(get(f"/store_credits/{customer_id}/balance"))
        except LightspeedError as e:
            out["store_credit"] = str(e)
    return out


@tool()
def get_customer_group_customers(customer_group_id: str, after: int = 0, page_size: int = 200) -> dict | list:
    """Customers in a customer group (version-cursor paged)."""
    return get(f"/customer_groups/{customer_group_id}/customers", after=after or None, page_size=page_size)


@tool()
def get_store_credit_report() -> dict | list:
    """Store credit report across all customers."""
    return _data(get("/store_credits/report"))


@tool()
def find_gift_card(card_number: str) -> dict | list:
    """A gift card by number, with balance, status and transactions."""
    return _data(get(f"/gift_cards/by_number/{urllib.parse.quote(card_number)}"))


@tool()
def list_gift_cards(status: str | None = None, page_size: int = 100, before: int | None = None) -> dict | list:
    """Gift cards, optionally by status (e.g. ACTIVE, VOIDED)."""
    return get("/gift_cards", status=status, page_size=page_size, before=before)


@tool()
def list_promotions(scope: str = "current_upcoming", name: str | None = None,
                    outlet_ids: list[str] | None = None, page_size: int = 200) -> dict | list:
    """Promotions. scope: current_upcoming (default), past or all. name matches part
    of the promotion name; outlet_ids keeps those linked to the given outlets."""
    return get("/promotions/search", scope=scope, name=name, outlet_id=outlet_ids,
               order_by="start_date", page_size=page_size)


@tool()
def get_promotion(promotion_id: str, include_products: bool = True, product_limit: int = 30) -> dict | list:
    """One promotion with its rules, promo codes and applicable products (counts plus
    the first product_limit of each product list)."""
    out = {"promotion": _data(get(f"/promotions/{promotion_id}"))}
    try:
        out["promo_codes"] = _data(get(f"/promotions/{promotion_id}/promocodes"))
    except LightspeedError as e:
        out["promo_codes"] = str(e)
    if include_products:
        prods = _data(get(f"/promotions/{promotion_id}/products", page_size=200)) or {}
        groups = prods.items() if isinstance(prods, dict) else [("products", prods)]
        out["products"] = {}
        for k, items in groups:
            if not isinstance(items, list):
                continue
            out["products"][k] = {"count": len(items), "first": [{
                "product_id": i.get("id"), "variant_name": i.get("variant_name") or i.get("name"),
                "sku": i.get("sku"), **{f: i[f] for f in ("price", "discount_price", "retail_price") if f in i},
            } for i in items[:product_limit]]}
    return out


@tool()
def get_price_book_products(price_book_id: str, product_ids: list[str] | None = None,
                            query: str | None = None, limit: int = 100) -> dict | list:
    """Prices in a price book (tax-exclusive): name, product_id, price, tax,
    markup. Filter by product_ids or query (name substring); capped at limit."""
    resp = get(f"/price_books/{price_book_id}/products",
               product_ids=",".join(product_ids) if product_ids else None)
    rows = _data(resp) or []
    if query:
        rows = [r for r in rows if query.lower() in (r.get("name") or "").lower()]
    keep = ("product_id", "name", "price", "retail_tax", "adjustment_type", "adjustment", "min_units", "max_units")
    return {"matches": len(rows), "products": [{k: r.get(k) for k in keep if k in r} for r in rows[:limit]]}


# ============================================================================
# Anything else
# ============================================================================

@tool()
def api_get(path: str, params: dict | None = None) -> dict | list:
    """Read any other X-Series endpoint with GET, e.g. /shifts, /services,
    /serialnumbers, /auditlog_events, /customer_taxes, /product_images/{id}.
    path is relative to /api/<version> and must start with '/'. params are query
    parameters (lists repeat the key). Reference: https://x-series-api.lightspeedhq.com/llms.txt"""
    if not re.fullmatch(r"/[A-Za-z0-9_\-./{}]*", path) or ".." in path:
        raise LightspeedError("path must look like /resource or /resource/{id}")
    if DENY.match(path):
        raise LightspeedError("that endpoint is not exposed by this connector")
    return get(path, **(params or {}))


def main():
    mcp.run()


if __name__ == "__main__":
    main()
