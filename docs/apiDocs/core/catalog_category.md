# Catalog Categories API Documentation

The tenant's own navigation tree over their catalog — "Menu → Starters → Vegetarian",
"Services → Diagnostics".

- **Base path**: `/api/v1/core/catalog-categories/`
- **Auth**: JWT. Machine (`VB_AGENT`) tokens are refused.
- **Scope**: tenant-filtered. Another tenant's category is **404**.

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/core/catalog-categories/` | Flat list, filter, search |
| `GET` | `/core/catalog-categories/tree/` | The whole tree, nested, in one response |
| `POST` | `/core/catalog-categories/` | Create |
| `GET` | `/core/catalog-categories/{id}/` | Retrieve one |
| `PATCH` | `/core/catalog-categories/{id}/` | Partial update, including re-parenting |
| `PUT` | `/core/catalog-categories/{id}/` | Full replace |
| `DELETE` | `/core/catalog-categories/{id}/` | Soft delete — refused while occupied |
| `POST` | `/core/catalog-categories/bulk_delete/` | Per-id result, not all-or-nothing |
| `GET` | `/core/catalog-categories/dropdown/` | `label`/`value` pairs, indented |

---

## Three levels, and why

Nesting is capped at **3 levels** (`depth` 0, 1, 2). Deep enough for "Menu → Starters →
Vegetarian", shallow enough that the whole tree is one query and the `/tree/` response is bounded
by construction.

`depth` is **stored and derived**, never accepted from a client. Re-parenting a category cascades
new depths to everything below it, and a move that would push a grandchild past the cap is
rejected — validating only the moved node's own depth would let the write half-succeed.

Categories are **optional on an item**. A tenant must be able to add their first product before
designing a taxonomy, so `category` is nullable and removing a section orphans its items rather
than deleting them.

---

## 1. List categories

`GET /api/v1/core/catalog-categories/`

Paginated. Ordered by `depth`, then `sort_order`, then name — tree order, because a category list
is browsed and "most recently added" puts a leaf above the root it belongs to.

| Parameter | Type | Description |
|---|---|---|
| `search` | string | Matches `name` and `description` |
| `ordering` | string | `name`, `sort_order`, `depth`, `created_at`, `updated_at`; `-` prefix for descending |
| `parent` | uuid | One level of children |
| `parent__isnull` | bool | `true` for the roots only |
| `depth` | int | `0`, `1` or `2` |
| `depth__gte` / `depth__lte` | int | Everything at or below a level |
| `is_active` | bool | |

### 200 OK

```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "60e2569a-…",
      "name": "Menu",
      "parent": null,
      "parent_name": null,
      "depth": 0,
      "sort_order": 0,
      "item_count": 0,
      "is_active": true,
      "created_at": "2026-08-11T08:48:33.093Z"
    },
    {
      "id": "fffc2988-…",
      "name": "Starters",
      "parent": "60e2569a-…",
      "parent_name": "Menu",
      "depth": 1,
      "sort_order": 0,
      "item_count": 12,
      "is_active": true,
      "created_at": "2026-08-11T08:48:33.101Z"
    }
  ],
  "errors": null,
  "meta": { "count": 2, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

`item_count` counts items filed **directly** under the section, not its children's. It is
annotated on the queryset, so listing 50 categories is one query rather than 101.

---

## 2. The tree

`GET /api/v1/core/catalog-categories/tree/`

Roots with their children inlined, ordered by `sort_order`. One request instead of one per level.
**Unpaginated** — the depth cap and the size of a real taxonomy make this bounded.

```json
{
  "status": true,
  "message": "Category tree retrieved successfully",
  "data": [
    {
      "id": "60e2569a-…",
      "name": "Menu",
      "depth": 0,
      "sort_order": 0,
      "is_active": true,
      "item_count": 0,
      "children": [
        {
          "id": "fffc2988-…",
          "name": "Starters",
          "depth": 1,
          "sort_order": 0,
          "is_active": true,
          "item_count": 12,
          "children": []
        }
      ]
    }
  ],
  "errors": null
}
```

---

## 3. Create

`POST /api/v1/core/catalog-categories/`

| Field | Type | Required | Validation |
|---|---|---|---|
| `name` | string | ✅ | Trimmed, 2–150. **Unique among siblings, case-insensitive** |
| `parent` | uuid | — | Same tenant. Omit or send `null` for a root |
| `description` | string | — | Free text |
| `sort_order` | int | — | Default `0`. Manual ordering among siblings |
| `is_active` | bool | — | Default `true`. Hides the section without deleting it |

`depth`, `item_count`, `child_count`, `tenant` and the audit fields are read-only.

### Uniqueness is per level

`"Red"` under *Wine* and `"Red"` under *Paint* are different things and both are allowed. Two roots
called *"Services"* are not.

That distinction needs two database constraints rather than one, because Postgres treats NULLs as
distinct inside a unique index — a single constraint including `parent` would silently permit two
identically-named roots, which is exactly the duplicate a user reports as a bug.

Soft-deleting a category frees its name for reuse.

### Error responses

| Status | When | Body `errors` |
|---|---|---|
| `400` | Duplicate sibling name | `{"name": ["A category called 'Menu' already exists at the top level."]}` or `"… already exists under 'Wine'."` |
| `400` | `parent` from another tenant | `{"parent": ["That category belongs to a different tenant."]}` |
| `400` | Would exceed 3 levels | `{"parent": ["Categories can be nested 3 levels deep at most."]}` |
| `400` | Move would push descendants too deep | `{"parent": ["This category has sub-categories of its own; moving it there would nest them deeper than 3 levels."]}` |
| `400` | Would create a loop | `{"parent": ["That category is below this one — moving it there would create a loop."]}` |
| `400` | Self-parent | `{"parent": ["A category cannot be its own parent."]}` |
| `401` | Missing or expired JWT | — |
| `403` | Machine (`VB_AGENT`) token | — |
| `404` | Category belongs to another tenant | — |

---

## 4. Update and re-parent

`PATCH /api/v1/core/catalog-categories/{id}/`

Sending a new `parent` moves the whole subtree. Depths are recomputed and cascaded down
automatically; you never send `depth`.

---

## 5. Delete

`DELETE /api/v1/core/catalog-categories/{id}/`

**Refused with `400` while the section still contains sub-categories or items.**

```json
{
  "status": false,
  "message": "This category still contains 1 item(s). Move or delete them first, or set is_active=false to hide the section while keeping its contents.",
  "data": null,
  "errors": { "detail": ["This category still contains 1 item(s). …"] }
}
```

The guard lives on the model, so it applies to the REST delete, `bulk_delete`, the admin and a
shell session alike. `PROTECT` on the foreign key cannot cover it: the row is *soft*-deleted, so
the database constraint never fires, and the items would be left pointing at a section the UI no
longer lists — silently vanishing from the catalog while still being sellable.

If you want the section hidden without moving anything, set `is_active: false`.

### Bulk delete is per-id, not all-or-nothing

`POST /api/v1/core/catalog-categories/bulk_delete/` with `{"ids": [...]}`.

Unlike the generic bulk delete elsewhere in this API, each category is checked individually — an
occupied one is reported and the rest are still deleted. Refusal here is expected and actionable,
so aborting the whole batch on the first one would be unhelpful.

```json
{
  "status": true,
  "message": "1 deleted, 1 refused",
  "data": {
    "deleted": ["a1b2c3d4-…"],
    "refused": {
      "60e2569a-…": "This category still contains 1 sub-categor(y/ies). …"
    }
  },
  "errors": null
}
```

A `400` with `"Some ids are invalid"` is returned if any id is not in the caller's tenant, and
nothing is deleted in that case.

---

## 6. Dropdown

`GET /api/v1/core/catalog-categories/dropdown/`

`label`/`value` pairs where the label carries the nesting as an indent:

```json
{ "data": [
  { "label": "Menu", "value": "60e2569a-…" },
  { "label": "— Starters", "value": "fffc2988-…" },
  { "label": "— — Vegetarian", "value": "9a1f77b2-…" }
] }
```

The indent is in the label rather than left to the client because a flat picker of section names
is ambiguous the moment two parents both have a "Red". Accepts `?limit=` up to 1000.

---

## Related

- [Catalog Items](./catalog_item.md)
- [Catalog Variants](./catalog_variant.md)
- [Catalog Item Media](./catalog_item_media.md)
