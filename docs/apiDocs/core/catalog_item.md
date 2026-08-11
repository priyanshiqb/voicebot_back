# Catalog Items API Documentation

Everything the tenant sells — goods, services, bundles and add-ons. The nameable, describable
half of an offering.

- **Base path**: `/api/v1/core/catalog-items/`
- **Auth**: JWT. Machine (`VB_AGENT`) tokens are refused on every action.
- **Scope**: every response is filtered to the caller's tenant. Another tenant's item is **404**,
  never 403.

> ## Price is not here
>
> Price, SKU, unit and stock live on [`/core/catalog-variants/`](./catalog_variant.md), because
> most real catalog lines have several prices under one name — Half and Full, Small/Medium/Large,
> 1 kg / 5 kg / 50 kg bag, Adult and Child.
>
> **Every item always has at least one variant.** If you create one without supplying any, a
> `Standard` variant priced `ON_REQUEST` is created for you. That is what lets every client read
> the price from `default_variant` without first asking whether the item has variants at all.
>
> Reads of this endpoint carry `default_variant` inline, so "what does this cost?" needs no
> second call.

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/core/catalog-items/` | List, filter, search |
| `POST` | `/core/catalog-items/` | Create — optionally with variants inline |
| `GET` | `/core/catalog-items/{id}/` | Retrieve one, with variants, media and add-ons |
| `PATCH` | `/core/catalog-items/{id}/` | Partial update |
| `PUT` | `/core/catalog-items/{id}/` | Full replace |
| `DELETE` | `/core/catalog-items/{id}/` | Soft delete — cascades to variants and media |
| `POST` | `/core/catalog-items/bulk_delete/` | Soft delete several by id |
| `GET` | `/core/catalog-items/dropdown/` | `label`/`value` pairs |
| `GET` | `/core/catalog-items/lookup/` | **Voice runtime** — resolve what a caller said |
| `GET` | `/core/catalog-items/tags/` | Distinct tags in use, with counts |
| `GET` | `/core/catalog-items/{id}/media/` | Confirmed media for one item |

---

## Two axes, not one

The single design decision that shapes this whole API:

| | Field | Answers |
|---|---|---|
| **What it is** | `item_kind` (here) | A good? A service? A bundle? An add-on? |
| **How it is charged** | `pricing_type`, `unit_of_measure`, `recurring_interval` (on the variant) | A flat price? Per hour? Per month? A percentage? |

A rented excavator is a **physical good** charged **hourly**. A monthly rent is a **physical
good** charged **recurringly**. A gym membership is a **service** charged **yearly**. Collapsed
into one `type` enum, each of those needs a new value and a migration; split, they are
combinations of values that already exist.

### `item_kind` values

| Value | Use for |
|---|---|
| `PHYSICAL_GOOD` | Anything tangible — retail stock, food, fertilizer, property |
| `DIGITAL_GOOD` | Ebooks, licences, downloads |
| `SERVICE` | Anything performed — consultations, haircuts, courses, memberships, loans |
| `BUNDLE` | A package of other items. *The component table is not built yet — the value is reserved.* |
| `ADD_ON` | Something picked **0..n times on top of** another item |
| `OTHER` | Genuinely unclassifiable. Should be rare |

**Required, with no default.** Forcing the choice is what stops a catalog filing everything under
whichever value happened to be the default.

### Add-on vs variant vs bundle

The distinction people get wrong most often:

| | Variant | Bundle | Add-on |
|---|---|---|---|
| Question it answers | *Which one* do you want? | Buy these *together* as one line | What do you want *on top*? |
| How many | exactly 1 | fixed contents | 0..n |
| Example | Small / Medium / Large | Full Body Checkup = 12 tests | Extra cheese, extended warranty |

Modelling an add-on as a variant makes it mutually exclusive with the thing it modifies — you
could have Large *or* extra cheese, never both.

---

## 1. List catalog items

`GET /api/v1/core/catalog-items/`

Paginated (`page`, `page_size`, max 100). Ordered by `sort_order` then newest first.

### Query parameters

| Parameter | Type | Description |
|---|---|---|
| `page` | int | 1-based page number |
| `page_size` | int | Rows per page, max 100 |
| `search` | string | Matches `name`, `item_code`, `brand`, `short_description`, `description` |
| `ordering` | string | One of `name`, `brand`, `sort_order`, `item_kind`, `created_at`, `updated_at`; prefix `-` for descending |
| `item_kind` | enum | `PHYSICAL_GOOD` \| `DIGITAL_GOOD` \| `SERVICE` \| `BUNDLE` \| `ADD_ON` \| `OTHER` |
| `category` | uuid | Items filed directly under this category |
| `brand` | string | Exact match |
| `event_template` | uuid | Items bookable through this event type |
| `is_featured` | bool | `true` \| `false` |
| `is_active` | bool | `true` \| `false` |
| `tags` | string | Comma-separated. Items carrying **ANY** of these tags |
| `tags_all` | string | Comma-separated. Items carrying **ALL** of these tags |
| `alias` | string | Exact alias match |
| `has_media` | bool | `true` = has at least one **confirmed** media file |
| `price_min` | decimal | Any variant priced at or above this |
| `price_max` | decimal | Any variant priced at or below this |

### 200 OK

```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "ffc111cd-bac8-4b9c-80bf-b391a04886e2",
      "name": "Paneer Butter Masala",
      "item_code": "",
      "item_kind": "PHYSICAL_GOOD",
      "item_kind_label": "Physical good",
      "category": "fffc2988-4dcf-4c23-9bda-528326630de0",
      "category_name": "Starters",
      "brand": "",
      "short_description": "Creamy tomato gravy with paneer.",
      "tags": ["bestseller", "veg"],
      "price": "₹220",
      "variant_count": 2,
      "primary_image_url": null,
      "is_featured": false,
      "is_active": true,
      "sort_order": 0,
      "created_at": "2026-08-11T08:48:33.118810Z"
    }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

`price` is the default variant's **rendered string**, not a number. A list showing
`"8.4% per annum"` next to `"₹50 base + ₹18 per kilometre"` cannot be built from an amount and a
currency code, and having two clients each invent their own formatting is how voice and dashboard
end up quoting differently.

---

## 2. Create an item

`POST /api/v1/core/catalog-items/`

### Request body

| Field | Type | Required | Validation |
|---|---|---|---|
| `item_kind` | enum | ✅ | One of the six values above. **No default** |
| `name` | string | ✅ | Trimmed, 2–200 characters. **Not unique** — see below |
| `category` | uuid | — | Must belong to the caller's tenant |
| `item_code` | string | — | Trimmed, ≤50. **Unique per tenant, case-insensitive**, when non-blank |
| `short_description` | string | — | ≤255. The one sentence the agent says out loud |
| `description` | string | — | Long form, unbounded |
| `brand` | string | — | ≤150 |
| `tags` | string[] | — | ≤25 entries, each ≤50 chars. Blanks dropped, de-duplicated case-insensitively |
| `aliases` | string[] | — | ≤25 entries, each ≤100 chars. Same cleanup |
| `fulfilment_modes` | enum[] | — | Any of `AT_BUSINESS`, `AT_CUSTOMER`, `REMOTE`, `DELIVERY`, `PICKUP`, `DINE_IN`. De-duplicated, order preserved |
| `attributes` | object | — | Must be a JSON **object**, ≤8192 bytes serialized |
| `event_template` | uuid | — | Same tenant, **and `item_kind` must be `SERVICE`** |
| `add_on_items` | uuid[] | — | ≤50. Each must be same-tenant, `item_kind=ADD_ON`, and not this item |
| `lead_time_minutes` | int | — | ≥0. Prep time, made-to-order lead time |
| `available_from` | date | — | `YYYY-MM-DD` |
| `available_until` | date | — | Must not be before `available_from` |
| `pronunciation_hint` | string | — | ≤150. TTS spelling for names the synthesiser mangles |
| `is_featured` | bool | — | Default `false` |
| `sort_order` | int | — | Default `0` |
| `is_active` | bool | — | Default `true` |
| `variants` | object[] | — | **Create only.** See below |

`tenant`, `created_by`, `updated_by`, `deleted_by`, `default_variant`, `media` and
`item_kind_label` are read-only and ignored if sent.

### `name` is deliberately not unique

Every other named model in this app (Agent, EventTemplate, Campaign, KnowledgeBase) has a unique
name. This one does not, and that is a decision rather than an oversight: those are
*configuration objects a user names*, and a catalog is a *data set* where two brands of
"Basmati Rice 5 kg" is a correct pair of rows. Uniqueness lives on `item_code` and on the
variant's `sku`, both optional.

### Variants inline (create only)

`POST` accepts a `variants` array so "add this product with its three sizes" is one request. Each
entry takes every field documented in [catalog_variant.md](./catalog_variant.md) **except**
`item`, `tenant` and the audit fields — the parent supplies those. Every pricing rule is validated
identically.

Additional checks on the array:

- At most `CATALOG_MAX_VARIANTS_PER_ITEM` (default 100) entries.
- **Variant labels must be distinct** — the label is what the agent says to tell options apart.
- At most one entry may set `is_default: true`.

`PUT` and `PATCH` **reject** `variants` with a 400 pointing at `/catalog-variants/`. Inline update
would need replace-or-upsert semantics over a priced collection, and the failure mode of guessing
wrong there is silently deleting a price.

### Example — a restaurant dish with two portion sizes

```json
{
  "item_kind": "PHYSICAL_GOOD",
  "name": "Paneer Butter Masala",
  "category": "fffc2988-4dcf-4c23-9bda-528326630de0",
  "short_description": "Creamy tomato gravy with paneer.",
  "aliases": ["paneer butter", "PBM"],
  "tags": ["bestseller", "veg"],
  "fulfilment_modes": ["DINE_IN", "DELIVERY"],
  "attributes": { "veg": true, "spice": "medium", "allergens": ["dairy"] },
  "lead_time_minutes": 20,
  "variants": [
    { "variant_label": "Half", "price": "220.00", "is_default": true },
    { "variant_label": "Full", "price": "380.00", "sort_order": 1 }
  ]
}
```

### 201 Created

```json
{
  "status": true,
  "message": "Created successfully",
  "data": {
    "id": "ffc111cd-bac8-4b9c-80bf-b391a04886e2",
    "tenant": { "id": "…", "name": "Acme" },
    "item_kind": "PHYSICAL_GOOD",
    "item_kind_label": "Physical good",
    "name": "Paneer Butter Masala",
    "category": { "id": "fffc2988-…", "name": "Starters", "depth": 1 },
    "aliases": ["paneer butter", "PBM"],
    "tags": ["bestseller", "veg"],
    "fulfilment_modes": ["DINE_IN", "DELIVERY"],
    "attributes": { "veg": true, "spice": "medium", "allergens": ["dairy"] },
    "variants": [
      { "id": "571cdfc3-…", "variant_label": "Half", "display_price": "₹220", "is_default": true },
      { "id": "8c2f0a11-…", "variant_label": "Full", "display_price": "₹380", "is_default": false }
    ],
    "default_variant": {
      "id": "571cdfc3-…", "variant_label": "Half", "sku": "",
      "display_price": "₹220", "is_default": true
    },
    "add_on_items": [],
    "media": [],
    "is_active": true,
    "created_at": "2026-08-11T08:48:33.118810Z"
  },
  "errors": null
}
```

### Error responses

| Status | When | Body `errors` |
|---|---|---|
| `400` | Duplicate `item_code` in this tenant | `{"item_code": ["Another active item already uses the code 'PRD-1'."]}` |
| `400` | `event_template` set on a non-service | `{"event_template": ["Only a service can be bookable. …"]}` |
| `400` | `category` or `event_template` from another tenant | `{"category": ["That category belongs to a different tenant."]}` |
| `400` | A non-`ADD_ON` passed in `add_on_items` | `{"add_on_items": ["Only items with item_kind=ADD_ON can be attached as add-ons. Rejected: Fries. …"]}` |
| `400` | `add_on_items` contains this item | `{"add_on_items": ["An item cannot be its own add-on."]}` |
| `400` | `attributes` over 8 KB or not an object | `{"attributes": ["attributes is 9014 bytes; the maximum is 8192. …"]}` |
| `400` | Too many tags / aliases | `{"tags": ["At most 25 tags are allowed; 31 were given."]}` |
| `400` | `available_until` before `available_from` | `{"available_until": ["The window must not end before it starts."]}` |
| `400` | `variants` sent on `PATCH`/`PUT` | `{"variants": ["Variants cannot be edited through this endpoint. …"]}` |
| `400` | Any variant pricing rule broken | See [catalog_variant.md](./catalog_variant.md#error-responses) |
| `401` | Missing or expired JWT | — |
| `403` | Machine (`VB_AGENT`) token | — |
| `404` | Item belongs to another tenant | — |

---

## 3. Retrieve, update, delete

`GET|PATCH|PUT|DELETE /api/v1/core/catalog-items/{id}/`

`GET` returns the full representation above, including `variants`, `media`, `add_on_items` and
`default_variant`.

`DELETE` is a **soft delete** and cascades: the item's variants and media are soft-deleted with
it. A variant of a delisted item left active is exactly the orphan that gets quoted at a caller
months later.

Deleting is not the only way to take something off sale — `is_active: false` delists it while
keeping its history, and is what you want for a seasonal item or a temporary stockout.

---

## 4. Lookup — the voice runtime endpoint

`GET /api/v1/core/catalog-items/lookup/?q=coke`

**This is what the agent calls mid-conversation.** Speech recognition returns the caller's words,
not the catalog's — so matching runs against `aliases` first, then the name, then SKUs, then
brand.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `q` | string | ✅ | What the caller said. **At least 2 characters** |
| `sellable_only` | bool | — | `true`/`1`/`yes` drops items with nothing sellable |

Returns at most **10** matches. A bot listing fifteen options is a bot the caller hangs up on.

Match order is deliberate rather than one OR'd query: an alias hit is a stronger signal than a
substring of a brand name, and the caller only hears the first few results.

### 200 OK

```json
{
  "status": true,
  "message": "1 match(es) for 'PBM'",
  "data": [
    {
      "id": "ffc111cd-…",
      "name": "Paneer Butter Masala",
      "item_kind": "PHYSICAL_GOOD",
      "brand": "",
      "short_description": "Creamy tomato gravy with paneer.",
      "aliases": ["paneer butter", "PBM"],
      "pronunciation_hint": "",
      "fulfilment_modes": ["DINE_IN", "DELIVERY"],
      "lead_time_minutes": 20,
      "attributes": { "veg": true, "spice": "medium" },
      "price": "₹220",
      "is_sellable": true,
      "variants": [
        { "id": "571cdfc3-…", "variant_label": "Half", "sku": "", "display_price": "₹220", "is_default": true },
        { "id": "8c2f0a11-…", "variant_label": "Full", "sku": "", "display_price": "₹380", "is_default": false }
      ],
      "add_ons": [{ "id": "3b9d97ad-…", "name": "Extra Cheese", "item_kind": "ADD_ON", "item_code": "" }]
    }
  ],
  "errors": null
}
```

**Prices are read live from the variant rows on every call, never from an embedding.** That is the
whole reason the catalog is a structured lookup rather than RAG content: a confidently stale price
is worse than no answer, and there is no way for the caller to tell the difference.

| Status | When |
|---|---|
| `400` | `q` missing or shorter than 2 characters — `"Pass ?q= with at least 2 characters."` |

---

## 5. Tag facets

`GET /api/v1/core/catalog-items/tags/`

Distinct tags in use with counts, so the filter UI does not have to page the whole catalog to
discover which tags exist. Sorted by count descending, then alphabetically.

```json
{
  "status": true,
  "message": "Tags retrieved successfully",
  "data": [
    { "tag": "bestseller", "count": 14 },
    { "tag": "organic", "count": 9 },
    { "tag": "veg", "count": 3 }
  ],
  "errors": null
}
```

---

## 6. Media for one item

`GET /api/v1/core/catalog-items/{id}/media/`

**Confirmed media only.** A `PENDING` row points at an object key that may have nothing behind it
— the client requested an upload URL and never finished — and serving those produces broken
images with presigned URLs that 404.

Response rows match the list shape in [catalog_item_media.md](./catalog_item_media.md).

---

## 7. Dropdown and bulk delete

`GET /api/v1/core/catalog-items/dropdown/` returns `label`/`value` pairs, where the label is
`"Name (ITEM-CODE)"` when a code is set and just the name otherwise. Accepts `?limit=` up to 1000.

`POST /api/v1/core/catalog-items/bulk_delete/` takes `{"ids": ["…", "…"]}` and soft-deletes them,
each cascading to its variants and media. Returns `400` with `"Some ids are invalid"` if any id is
not in the caller's tenant — nothing is deleted in that case.

---

## Related

- [Catalog Variants](./catalog_variant.md) — price, SKU, unit, stock
- [Catalog Categories](./catalog_category.md) — the navigation tree
- [Catalog Item Media](./catalog_item_media.md) — the two-step upload flow
- [Event Templates](./event_template.md) — what a bookable service points at
