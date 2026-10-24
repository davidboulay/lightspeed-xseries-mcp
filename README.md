<div align="center">

<img src="docs/icon.svg" alt="" width="104" height="104">

<h1>Lightspeed X-Series MCP</h1>

**Ask Claude about your Lightspeed Retail stores.** Sales, stock, products and customers,<br>
through an MCP server that **cannot write** to your account.

[![CI](https://img.shields.io/github/actions/workflow/status/davidboulay/lightspeed-xseries-mcp/ci.yml?branch=main&style=flat-square&label=tests&labelColor=1c1c1e)](https://github.com/davidboulay/lightspeed-xseries-mcp/actions/workflows/ci.yml)
[![Read-only](https://img.shields.io/badge/read--only-by%20construction-FF8A3D?style=flat-square&labelColor=1c1c1e)](#read-only-by-construction)
[![Python](https://img.shields.io/badge/python-3.12%2B-E5484D?style=flat-square&labelColor=1c1c1e)](pyproject.toml)
[![License](https://img.shields.io/github/license/davidboulay/lightspeed-xseries-mcp?style=flat-square&color=6b7280&labelColor=1c1c1e)](LICENSE)

[**Get started**](#get-started) · [What you can ask](#what-you-can-ask) · [Tools](#tools) · [Read-only](#read-only-by-construction) · [Hosting for a team](#hosting-for-a-team) · [Security](SECURITY.md)

</div>

<br>

[Lightspeed Retail X-Series](https://www.lightspeedhq.com/pos/retail/) (formerly
Vend) holds a store's whole day: every sale, every stock movement, every
customer. This server puts that data in front of Claude, or any other MCP
client, so you can ask a question in plain words and get the number back:
*"How did each store do yesterday?"*, *"Where do we still have the large one in
navy?"*, *"Which promotions start next week?"*

It is **read-only in code**. Lightspeed's personal tokens carry no scopes, so a
token that can read your sales can also void them. This server never asks the
API to change anything, and a test fails the build if a write ever creeps in.

## What you can ask

Claude picks the tools; you just ask. Things that work well:

- **Revenue:** "Sales by outlet for last week, excluding tax." · "Daily takings
  in March as a table." · "Top 20 products by units this month."
- **Stock:** "How many of SKU 1234-NV are on hand, by store?" · "Show every size
  and color of the Weekender with stock per outlet." · "What's low at the
  warehouse?"
- **Sales lookups:** "Find invoice 10482." · "Which sales did Sam ring up on
  Saturday?" · "Show parked and on-account sales from this week."
- **Customers and loyalty:** "Look up jane@example.com: spend this year, store
  credit, address." · "Check gift card 6006 1234 5678."
- **Merchandising:** "Which promotions run this month and which products do they
  cover?" · "Prices in the wholesale price book for the Weekender."
- **Operations:** "Open stock transfers between stores." · "Purchase orders still
  waiting on delivery." · "Payments by type on register 2 today."

Dates are **local store dates**: "yesterday" means yesterday in the retailer's
time zone, not UTC, and the server converts for you.

## Features

- **35 tools** covering sales, revenue summaries, products, variant families,
  inventory, transfers, purchase orders, customers, store credit, gift cards,
  promotions and price books, plus a guarded `api_get` for anything else.
- **Answers, not just endpoints.** `sales_summary` groups revenue by outlet, day,
  staff member or product in one call. `search_products` matches any mix of
  name, variant, SKU and barcode words. `get_inventory` rolls a whole variant
  family up into one stock table.
- **Readable output.** Outlet, staff and product ids come back with their names,
  and sales come back as compact rows unless you ask for the full object.
- **Sized for a chat window.** Variant families, price books and promotion
  product lists can run past a megabyte. Those tools return summaries by
  default, with `full=True` or a filter when you need everything.
- **Local time done right.** Date ranges are converted from the retailer's time
  zone to the UTC bounds the API wants, so day totals match the Lightspeed
  reports.
- **Polite to the API.** Reference data is cached (outlets and staff for an hour,
  the product catalog for 15 minutes), and a `429` is retried once after
  `Retry-After`.
- **Standard library HTTP.** The only dependency is the MCP SDK.

## Get started

### 1. Make a personal token

In Lightspeed Retail, open **Setup → Personal tokens** and add one. Use an
account that can see every outlet you want to query, because the token sees
exactly what that user sees. Lightspeed lists personal tokens as a plan feature,
so check your plan if the menu is missing.

Your **domain prefix** is the first part of your store address:
`mystore` for `mystore.retail.lightspeed.app`.

### 2. Add it to your MCP client

The server runs over **stdio**. With [`uv`](https://docs.astral.sh/uv/)
installed, `uvx` fetches and runs it without a separate install step.

**Claude Code**

```bash
claude mcp add lightspeed \
  -e LIGHTSPEED_DOMAIN_PREFIX=mystore \
  -e LIGHTSPEED_TOKEN=your-token \
  -- uvx --from git+https://github.com/davidboulay/lightspeed-xseries-mcp lightspeed-xseries-mcp
```

**Claude Desktop** (`claude_desktop_config.json`), **Cursor** and other
JSON-configured clients:

```json
{
  "mcpServers": {
    "lightspeed": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/davidboulay/lightspeed-xseries-mcp", "lightspeed-xseries-mcp"],
      "env": {
        "LIGHTSPEED_DOMAIN_PREFIX": "mystore",
        "LIGHTSPEED_TOKEN": "your-token"
      }
    }
  }
}
```

**Codex**

```bash
codex mcp add lightspeed \
  --env LIGHTSPEED_DOMAIN_PREFIX=mystore --env LIGHTSPEED_TOKEN=your-token \
  -- uvx --from git+https://github.com/davidboulay/lightspeed-xseries-mcp lightspeed-xseries-mcp
```

Then ask *"What's the retailer name and currency on Lightspeed?"*. If you get
an answer, you're connected.

> **The token sits in your client's config file in plain text.** That's normal
> for stdio MCP servers, but it means the file deserves the same care as the
> token. To share access without handing the token around, see
> [Hosting for a team](#hosting-for-a-team).

### Several stores

One process serves one retailer account. For several, add the server once per
store under different names (`lightspeed-us`, `lightspeed-uk`, …), each with its
own prefix and token. Claude sees them as separate tool sets and can compare
them.

### Configuration

| Variable | Required | Default | |
|---|---|---|---|
| `LIGHTSPEED_DOMAIN_PREFIX` | yes | | `mystore` in `mystore.retail.lightspeed.app` |
| `LIGHTSPEED_TOKEN` | yes | | personal token from *Setup → Personal tokens* |
| `LIGHTSPEED_API_VERSION` | no | `2026-10` | X-Series [date-based API version](https://x-series-api.lightspeedhq.com/). Each version is supported for about a year, so bump it yearly. |

## Tools

| Area | Tools |
|---|---|
| **Account** | `get_retailer` · `list_outlets` · `list_registers` · `list_users` · `list_payment_types` · `list_taxes` · `list_reference` (brands, suppliers, tags, categories, product types, variant attributes, customer groups, price books, outlet taxes, button layouts) |
| **Products** | `search_products` · `get_product` · `get_product_family` · `list_products` |
| **Stock** | `get_inventory` · `list_inventory_levels` · `list_stock_adjustments` · `list_consignments` · `get_consignment` · `list_purchase_orders` · `get_purchase_order` |
| **Sales** | `sales_summary` · `search_sales` · `get_sale` · `get_user_sale_totals` · `get_register_payments_summary` · `list_quotes` · `list_fulfillments` |
| **Customers** | `search_customers` · `get_customer` · `get_customer_group_customers` · `get_store_credit_report` · `find_gift_card` · `list_gift_cards` |
| **Pricing** | `list_promotions` · `get_promotion` · `get_price_book_products` |
| **Anything else** | `api_get`: GET any other endpoint (`/shifts`, `/serialnumbers`, `/auditlog_events`, …) |

The ones to know:

- **`sales_summary`**: revenue for a local date range grouped by `outlet`,
  `day`, `user`, `product` or `none`, with totals excluding and including tax,
  the average sale and, per product, units. It counts closed sales by default,
  includes returns as negatives, and stops at 20,000 sales per call, setting
  `truncated` when you should narrow the range.
- **`search_products`**: every word must match somewhere in the name, variant,
  SKU or barcodes, so "navy large weekender" finds the right variant. Inactive
  products are hidden unless you ask.
- **`get_inventory`**: on-hand stock per outlet for one variant, or with
  `include_variants=True` for the whole family, totaled per variant.

## Read-only by construction

A personal token can do anything its user can, including deleting products and
voiding sales. So read-only can't be a setting. It has to be the code:

- Every tool sends **`GET`**. The two exceptions are `/inventory` and
  `/inventory_levels`, which are reads that take their filters as a `POST` body.
- There are **no write tools**. Every tool carries MCP's `readOnlyHint`, so
  clients that ask before running non-read tools never need to ask.
- **`api_get`** accepts plain resource paths only (no `..`, no query string in
  the path) and refuses integration plumbing that can carry credentials or
  partner data: `/webhooks`, `/partner`, `/channel_requests`,
  `/security_events`.
- **[`tests/test_read_only.py`](tests/test_read_only.py)** parses the source and
  fails if any request uses a method other than `GET` or one of the two
  read-only `POST`s. It also checks every tool's annotations and the `api_get`
  deny list. CI runs it on every push.

Read-only still means **readable**: whoever can call these tools can see
customer contact details, staff names, and cost fields such as average cost and
supplier price. Give access accordingly.

## Hosting for a team

stdio keeps the token on one machine. To give colleagues access **without giving
them the token**, run the server on a host you control behind an
**authenticating** proxy that speaks remote MCP. For example,
[`mcp-auth-proxy`](https://github.com/sigbit/mcp-auth-proxy) adds OAuth sign-in
(Google, GitHub, …) with a domain allow-list:

```bash
mcp-auth-proxy --external-url https://lightspeed.example.com \
  --google-client-id … --google-client-secret … --google-allowed-workspaces example.com \
  -- lightspeed-xseries-mcp
```

People then add `https://lightspeed.example.com/mcp` as a custom connector
(claude.ai → *Settings → Connectors*), sign in with their work account, and never
see the token.

**Never expose the server without authentication.** Don't publish a bare HTTP
port or a tunnel to it: anyone who finds the URL could read your sales and
customers. Pin dependencies when you deploy. [`requirements.lock`](requirements.lock)
is a full, tested freeze.

## API quirks it handles

Found against live data on API version `2026-10`, and worth knowing if you call
`api_get` yourself:

- **Two sale shapes.** `/search?type=sales` returns the older 2.x shape
  (`total_price`, `total_tax`, `sale_date`, `outlet_id`, line `price_total`),
  while `/sales/{id}` returns the date-based one (`totals`, `source`). The
  server reads both.
- **Product names live on the family.** Fetch products with
  `includes[]=families`. `variant_name` already contains the product name.
- **Family stock.** `/inventory` with `variants: true` wants the *root* product
  id and returns nothing for a variant id, so family stock goes through
  `/inventory_levels` with `product_ids`.
- **`/product_categories`** nests its list under `data.categories`.
- **`/promotions/search`** date filters mean "starts after". Use
  `scope=current_upcoming` for what's running now.
- **Retailer settings.** `currency` is an object (use `currency.code`), and the
  time zone may come back as `timezone` or `time_zone`.

## Limitations

- **Read-only, permanently.** There's no plan to add writes. Use Lightspeed
  itself for changes.
- **Personal tokens only.** OAuth would need a registered Lightspeed developer
  app. A token is simpler for a single retailer, but it's tied to one user's
  access.
- **Big ranges are capped.** `sales_summary` stops at 20,000 sales and
  `search_sales` at its `limit`. Split long periods by month.
- **Caches are per process.** A product added in the last 15 minutes may not
  appear in `search_products` yet. `get_product` by SKU is always live.
- **One `429` retry.** Under heavy load a call can still fail with Lightspeed's
  rate-limit error. Ask again a moment later.

## Project layout

```
src/lightspeed_xseries_mcp/
  server.py          the whole server: HTTP client, caches, 35 tools
tests/
  test_read_only.py  read-only guarantee, no network or token needed
requirements.lock    full tested dependency freeze, for deployments
docs/icon.svg
```

## Development

```bash
git clone https://github.com/davidboulay/lightspeed-xseries-mcp.git && cd lightspeed-xseries-mcp
uv run --with-requirements requirements.lock --with-editable . python -m unittest discover -s tests -v
```

To try it against a real store, run it under the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
LIGHTSPEED_DOMAIN_PREFIX=mystore LIGHTSPEED_TOKEN=… \
  npx @modelcontextprotocol/inspector uv run lightspeed-xseries-mcp
```

When you add a tool, keep it a read. The test will tell you if it isn't.

## License

MIT. An independent project, not affiliated with or endorsed by Lightspeed
Commerce Inc. "Lightspeed" and "X-Series" are its trademarks.
