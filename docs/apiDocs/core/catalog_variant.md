# Catalog Variants API Documentation

The **priceable, sellable unit** of a catalog item. Price, SKU, unit of measure, tax and stock all
live here.

- **Base path**: `/api/v1/core/catalog-variants/`
- **Auth**: JWT. Machine (`VB_AGENT`) tokens are refused.
- **Scope**: tenant-filtered. Another tenant's variant is **404**.

> ## Why a separate table
>
> Most real catalog lines have more than one price under one name: Half and Full, Small/Medium/
> Large, 1 kg / 5 kg / 50 kg bag, Adult and Child, Peak and Off-Peak.
>
> The alternative — a price on the item with variants that "override" it — gives every client two
> places to read a price from, and the one that reads the wrong one is the voice agent quoting a
> caller. Always-a-variant costs one join and removes the branch entirely.
>
> **Every item has at least one.** Items created without variants get a `Standard` one priced
> `ON_REQUEST`.

> ## Nothing here decrements stock
>
> `stock_quantity` is a number the **tenant states**, not a balance this module computes. There are
> no orders in this system. `min_order_quantity` is likewise a sentence the agent can say, not a
> rule anything enforces.

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/core/catalog-variants/` | List, filter, search — the pricing table |
| `POST` | `/core/catalog-variants/` | Add a priced option to an item |
| `GET` | `/core/catalog-variants/{id}/` | Retrieve one |
| `PATCH` | `/core/catalog-variants/{id}/` | Partial update — **the repricing endpoint** |
| `PUT` | `/core/catalog-variants/{id}/` | Full replace |
| `DELETE` | `/core/catalog-variants/{id}/` | Soft delete — promotes a successor default |
| `POST` | `/core/catalog-variants/{id}/set-default/` | Make this the item's default |
| `POST` | `/core/catalog-variants/bulk_delete/` | Soft delete several by id |
| `GET` | `/core/catalog-variants/dropdown/` | `label`/`value` pairs |

---

## Pricing shapes

`pricing_type` is the second of the module's two axes — `item_kind` says *what it is*, this says
*how it is charged*.

| `pricing_type` | Requires | Renders as | For |
|---|---|---|---|
| `FIXED` | `price` | `₹380` | The ordinary case |
| `FROM` | `price` | `from ₹50,000` | Project work scoped per enquiry |
| `RANGE` | `price` **and** `max_price` | `₹4,000–₹9,000` | Both ends known |
| `RECURRING` | `price` **and** `recurring_interval` | `₹12,000 per year` | Memberships, rent, AMCs, retainers |
| `RATE` | `price` (0–100) | `8.4% per annum on the loan amount` | Loans, brokerage, commission |
| `ON_REQUEST` | nothing | `Price on request` | Quote-only |
| `FREE` | nothing | `Free` | Lead-gen offers, samples |

### `base_price` cuts across all of them

Set it and the total reads `base_price + (price × quantity)` — `price` becomes the **per-unit
rate**. That is how a taxi (₹50 base + ₹18/km) and a call-out trade (₹300 visit + ₹150/unit)
actually quote, and it is not expressible with a single amount.

```json
{ "base_price": "50.00", "price": "18.00", "unit_of_measure": "KM" }
```
→ `"₹50 base + ₹18 per kilometre"`

### `display_price`

Every representation carries a **rendered** `display_price` composed on the server. Read it rather
than formatting the amount yourself: the composite and rate shapes above are exactly where two
independent implementations drift, and a dashboard and a voice agent quoting differently is a
support ticket nobody can reproduce.

---

## Units of measure

28 values, grouped:

| Group | Values |
|---|---|
| Count | `PIECE` `DOZEN` `PACK` `BOX` |
| Weight & volume | `GRAM` `KG` `QUINTAL` `TONNE` `ML` `LITRE` |
| Distance & area | `METRE` `KM` `SQ_FT` `SQ_METRE` `ACRE` |
| Time | `MINUTE` `HOUR` `DAY` `NIGHT` `WEEK` `MONTH` `YEAR` |
| Service | `SESSION` `VISIT` `TRIP` `PERSON` |
| Rate | `PERCENT` |
| Escape hatch | `OTHER` |

`unit_of_measure` is for logic; **`unit_label` is for humans and TTS**. Set it to the exact words
the invoice line and the spoken sentence should use — `"per 50 kg bag"`, `"per plate"`,
`"% per annum of loan amount"`. Left blank, the unit's own display name is used.

---

## 1. List variants

`GET /api/v1/core/catalog-variants/`

Paginated. Ordered by item then `sort_order`.

| Parameter | Type | Description |
|---|---|---|
| `search` | string | Matches `variant_label`, `sku`, `barcode`, the item's `name` and `item_code` |
| `ordering` | string | `variant_label`, `sku`, `price`, `sort_order`, `stock_quantity`, `created_at`, `updated_at`; `-` prefix for descending |
| `item` | uuid | One product's options |
| `item__item_kind` | enum | Variants of goods, services, add-ons… |
| `item__category` | uuid | Everything priced in one section |
| `pricing_type` | enum, `in` | e.g. `?pricing_type__in=ON_REQUEST,FROM` — the "needs a quote" list |
| `availability_status` | enum, `in` | e.g. `?availability_status=OUT_OF_STOCK` — the restock list |
| `unit_of_measure` | enum | |
| `currency` | string | ISO-4217 |
| `is_default` | bool | |
| `track_inventory` | bool | |
| `is_active` | bool | |
| `stock_quantity` | int, `gte`, `lte` | `?track_inventory=true&stock_quantity__lte=5` — low stock |
| `price` | decimal, `gte`, `lte` | `?price__gte=1000&price__lte=5000` |

### 200 OK

```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "571cdfc3-…",
      "item": "ffc111cd-…",
      "item_name": "Paneer Butter Masala",
      "variant_label": "Half",
      "sku": "",
      "is_default": true,
      "pricing_type": "FIXED",
      "price": "220.00",
      "currency": "INR",
      "unit_of_measure": "PIECE",
      "display_price": "₹220",
      "availability_status": "IN_STOCK",
      "track_inventory": false,
      "stock_quantity": null,
      "is_sellable": true,
      "is_active": true,
      "created_at": "2026-08-11T08:48:33.128449Z"
    }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

### `is_sellable`

**The single definition of "can we sell this right now?".** It folds in the variant's `is_active`
and `availability_status`, its stock when `track_inventory` is on, *and* the parent item's
`is_active` and seasonal window. Read it instead of assembling the answer yourself — otherwise
every consumer has to remember all five conditions, and one will not.

---

## 2. Create a variant

`POST /api/v1/core/catalog-variants/`

### Request body

**Identity**

| Field | Type | Required | Validation |
|---|---|---|---|
| `item` | uuid | ✅ | Must belong to the caller's tenant |
| `variant_label` | string | ✅ | Trimmed, non-blank, ≤120. Use `"Standard"` if there is only one |
| `options` | object | — | JSON object, ≤8192 bytes. e.g. `{"size": "L", "colour": "Red"}` |
| `sku` | string | — | ≤64. **Unique per tenant, case-insensitive**, when non-blank |
| `barcode` | string | — | ≤64. **Not unique** — two rows may resell the same barcoded product |
| `is_default` | bool | — | Default `false`. Setting `true` demotes the current default atomically |
| `sort_order` | int | — | Default `0`. Small before Large, not alphabetical |
| `is_active` | bool | — | Default `true` |

**Pricing**

| Field | Type | Required | Validation |
|---|---|---|---|
| `pricing_type` | enum | — | Default `FIXED`. See the table above |
| `price` | decimal(14,2) | conditional | ≥0. Required for `FIXED`, `FROM`, `RECURRING`, `RATE`. For `RATE` it is a **percentage, 0–100** |
| `base_price` | decimal(14,2) | — | ≥0. The fixed component of a composite price |
| `max_price` | decimal(14,2) | conditional | ≥`price`. Required for `RANGE` |
| `compare_at_price` | decimal(14,2) | — | ≥0. MRP / strike-through |
| `currency` | string(3) | — | ISO-4217, upper-cased for you. Defaults to `CATALOG_DEFAULT_CURRENCY` (`INR`) |
| `unit_of_measure` | enum | — | Default `PIECE` |
| `unit_label` | string | — | ≤50. The words for the spoken quote |
| `recurring_interval` | enum | conditional | `DAY` \| `WEEK` \| `MONTH` \| `QUARTER` \| `YEAR`. **Required for `RECURRING`, and must be null otherwise** |
| `recurring_interval_count` | int | — | Default `1`. "every 3 months" is `MONTH` × 3 |
| `deposit_amount` | decimal(14,2) | — | ≥0. Refundable deposit for rentals and hire |
| `duration_minutes` | int | — | **Must be null if the item has an `event_template`** |
| `tax_rate_percentage` | decimal(5,2) | — | 0–100 |
| `is_tax_inclusive` | bool | — | **Default `true`** — the Indian MRP convention |
| `tax_code` | string | — | ≤20. HSN (goods) / SAC (services). Free text |

**Availability**

| Field | Type | Required | Validation |
|---|---|---|---|
| `availability_status` | enum | — | `IN_STOCK` (default) \| `LOW_STOCK` \| `OUT_OF_STOCK` \| `PREORDER` \| `MADE_TO_ORDER` \| `SEASONAL` \| `DISCONTINUED` |
| `track_inventory` | bool | — | Default `false` |
| `stock_quantity` | int | — | ≥0. **Must be null unless `track_inventory` is true** |
| `low_stock_threshold` | int | — | ≥0 |
| `min_order_quantity` | decimal(12,3) | — | ≥0. Stated, not enforced |
| `quantity_increment` | decimal(12,3) | — | ≥0. "sold in multiples of 10" |

`tenant`, `item_name`, `item_kind`, `display_price`, `unit_phrase`, `is_quotable`, `is_sellable`
and the `*_label` fields are read-only.

### Why `is_tax_inclusive` defaults to `true`

The number the tenant types is the number the caller pays. Getting this default wrong means every
quoted price is silently wrong by the tax rate, which nobody notices until a customer disputes an
invoice.

### `availability_status` is stated, never derived

It is **not** computed from `stock_quantity`. The two disagree constantly in reality — "3 left but
all reserved", "out of stock but taking pre-orders" — and a derived column would silently
overwrite what the tenant said. `is_sellable` combines them at read time instead.

### Examples

**A loan quoted as a rate**

```json
{
  "item": "…", "variant_label": "Salaried",
  "pricing_type": "RATE", "price": "8.40",
  "unit_of_measure": "PERCENT", "unit_label": "per annum on the loan amount"
}
```
→ `"8.4% per annum on the loan amount"`

**A rental with a deposit and a minimum**

```json
{
  "item": "…", "variant_label": "With operator",
  "price": "2500.00", "unit_of_measure": "HOUR",
  "deposit_amount": "25000.00", "min_order_quantity": "4.000"
}
```
→ `"₹2,500 per hour"`

**Tracked retail stock**

```json
{
  "item": "…", "variant_label": "UK 9", "sku": "NAM-9",
  "price": "8995.00", "track_inventory": true, "stock_quantity": 4,
  "low_stock_threshold": 3
}
```

### Error responses

Every rule below is enforced **twice** — as a field error here and as a CHECK constraint in the
database. The pair is what makes a bad price a clean 400 for an API client and still impossible
for a bulk importer or a data fix.

| Status | When | Body `errors` |
|---|---|---|
| `400` | `FIXED`/`FROM`/`RECURRING`/`RATE` with no `price` | `{"price": ["A price is required when pricing_type is FIXED. Use ON_REQUEST if the figure is only given on enquiry."]}` |
| `400` | `RATE` outside 0–100 | `{"price": ["A RATE is a percentage and must be between 0 and 100. 8.4 means 8.4%, not ₹8.40."]}` |
| `400` | `RECURRING` with no interval | `{"recurring_interval": ["A RECURRING price needs an interval — DAY, WEEK, MONTH, QUARTER or YEAR."]}` |
| `400` | Interval set on a non-`RECURRING` row | `{"recurring_interval": ["An interval only applies to RECURRING pricing, not FIXED. …"]}` |
| `400` | `RANGE` with no `max_price` | `{"max_price": ["A RANGE price needs an upper bound."]}` |
| `400` | `max_price` below `price` | `{"max_price": ["The upper bound must not be below the lower bound."]}` |
| `400` | Any negative money field | `{"price": ["Must not be negative."]}` |
| `400` | `tax_rate_percentage` outside 0–100 | `{"tax_rate_percentage": ["Must be between 0 and 100."]}` |
| `400` | `stock_quantity` set with `track_inventory: false` | `{"stock_quantity": ["Stock is only meaningful when track_inventory is true. …"]}` |
| `400` | Duplicate `sku` in this tenant | `{"sku": ["Another active variant already uses the SKU 'ABC-1'."]}` |
| `400` | `duration_minutes` set on an event-template-backed item | `{"duration_minutes": ["This item is booked through an event template, which already defines its duration. …"]}` |
| `400` | `item` from another tenant | `{"item": ["That item belongs to a different tenant."]}` |
| `400` | Item already at the variant cap | `{"item": ["'Deluxe Room' already has 100 variants; the maximum is 100. If you are modelling one row per date, that is not what variants are for."]}` |
| `400` | `currency` not a 3-letter code | `{"currency": ["Currency must be a three-letter ISO-4217 code, e.g. INR or USD."]}` |
| `401` | Missing or expired JWT | — |
| `403` | Machine (`VB_AGENT`) token | — |
| `404` | Variant belongs to another tenant | — |

---

## 3. Update — the repricing endpoint

`PATCH /api/v1/core/catalog-variants/{id}/`

Coherence rules are evaluated against the **effective** row, not just the fields you send. Sending
`pricing_type: "RANGE"` alone on a row with no `max_price` is rejected — a partial update cannot
slip past a two-field rule by omitting one of them.

Setting `is_default: true` demotes the item's current default inside the same transaction, so the
item is never momentarily left with none or two.

---

## 4. Set default

`POST /api/v1/core/catalog-variants/{id}/set-default/`

No body. Makes this the variant quoted when a caller does not specify an option, demoting the
current one atomically. Idempotent. Returns the full variant.

---

## 5. Delete

`DELETE /api/v1/core/catalog-variants/{id}/`

Soft delete. **If it was the default, the next variant by `sort_order` is promoted** — otherwise
the item would answer "what does this cost?" with nothing while still having two perfectly good
prices.

The `is_default` flag is also cleared on the way out, so restoring the row later does not collide
with the successor that took its place.

---

## Related

- [Catalog Items](./catalog_item.md) — the nameable half, and the `lookup` endpoint
- [Catalog Categories](./catalog_category.md)
- [Catalog Item Media](./catalog_item_media.md)
