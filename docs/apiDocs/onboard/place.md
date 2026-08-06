# Place API Documentation

The leaf of the [geography hierarchy](./country.md#the-hierarchy): a city or locality paired with a
postal code. One row per **(name, postal code)** combination, so a city with several postal codes
appears several times.

**Auth**: a **JWT** *or* a **temporary onboarding token**. Pagination, timeout, and auth follow the
[geography conventions](./country.md#geography-conventions) — **max page size 25**, **5-second
timeout**.

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/onboard/places/dropdown/` | Search cities, optionally narrowed by any parent level |
| `GET` | `/onboard/places/{id}/` | Full detail, with the whole parent chain expanded |

A third endpoint, [`/places/postal-codes/dropdown/`](./postal_code_dropdown.md), searches the same
table but is optimised for postal-code-first entry.

> **Every parent filter is optional here**, unlike [Admin1–3](./admin1.md). You can query places by
> country alone, which is what makes it possible to skip missing administrative levels.

---

## 1. Place Dropdown

- **Path**: `GET /api/v1/onboard/places/dropdown/`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `country_id` | UUID | Narrow to one country. | Optional. |
| `admin1_id` | UUID | Narrow to one state. | Optional. |
| `admin2_id` | UUID | Narrow to one district. | Optional. |
| `admin3_id` | UUID | Narrow to one sub-district. | Optional. |
| `postal_code` | string | **Exact** match on the postal code, spaces included. | Optional. Matched literally — `SW1A 1AA` will not match a stored `SW1A1AA`. |
| `search` | string | Matches the **place name** (substring, case-insensitive) **OR** the postal code (prefix, spaces ignored). | Optional. See below. |
| `page` | integer | Page number. Default `1`. | — |
| `page_size` | integer | Results per page. Default `10`. | **Capped at 25.** |

**200 OK**
```json
{
  "status": true,
  "message": "success",
  "data": [
    { "id": "f5a6b7c8-…", "cities": "Bengaluru", "postal_code": "560001" }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | UUID | The place id. Use with [Place Detail](#2-place-detail). |
| `cities` | string | The place name. **Note the plural key** — it is a single name, not a list. |
| `postal_code` | string | The postal code for this row. |

> **This dropdown does not use `label` / `value`.** It returns `id`, `cities`, and `postal_code`,
> unlike every other dropdown in the API — so a generic picker component will not bind to it
> without mapping.

> **`search` covers both name and postal code in one parameter.** Spaces are stripped from the
> postal-code side, so `560 001` matches `560001`. The name side is a plain substring match with
> spaces preserved.

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more places. An empty `data` array is normal — **there is no `NO_FURTHER_DATA` here**. | `success` |
| **401** | No valid JWT or onboarding token. | `Authentication credentials were not provided.` |
| **408** | Query exceeded 5 seconds. | `Query timeout exceeded. Please refine your search parameters.` |

> **Calling with no filters at all is the most likely way to hit the 408.** The table holds millions
> of rows; always send at least `country_id` or `search`.

---

## 2. Place Detail

The full record with its whole parent chain resolved — what you call once the user has picked a
place, to fill in state and district automatically.

- **Path**: `GET /api/v1/onboard/places/{id}/`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `id` * | UUID | The place id, from either dropdown. | Path parameter. Must be a well-formed UUID or the route does not match (**404**). |

**200 OK**
```json
{
  "status": true,
  "message": "success",
  "data": {
    "id": "f5a6b7c8-…",
    "name": "Bengaluru",
    "postal_code": "560001",
    "country": { "id": "b1c2d3e4-…", "code": "IN", "name": "India", "dial_code": "+91", "format_mask": "XXXXX XXXXX" },
    "admin1": { "id": "c2d3e4f5-…", "code": "KA", "name": "Karnataka" },
    "admin2": { "id": "d3e4f5a6-…", "code": "BLR", "name": "Bengaluru Urban" },
    "admin3": null
  },
  "errors": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `name` | string | The place name. **Called `name` here, but `cities` in the dropdown.** |
| `postal_code` | string | The postal code. |
| `country` | object | Always present — the FK is non-nullable. |
| `admin1` / `admin2` / `admin3` | object \| **null** | The parent chain. **Each is independently nullable**, because coverage varies by country. |

> **Use this to auto-fill the address form.** One call after the user picks a city gives you state,
> district, and country without walking the hierarchy downward.

> **Guard every level for `null`.** A place can have a country and no state, or a state and no
> district. `admin3` is null more often than not.

> **The key for the place name differs between endpoints** — `cities` in the dropdown, `name`
> here. Not a typo in this document; the serializers genuinely disagree.

| Status | When | `message` |
|---|---|---|
| **200** | Found. | `success` |
| **401** | No valid JWT or onboarding token. | `Authentication credentials were not provided.` |
| **404** | No place with that id, or the path segment is not a UUID. | `Not found.` |
| **408** | Query exceeded 5 seconds. | `Query timeout exceeded. Please refine your search parameters.` |

---

## Choosing an entry point

| The user starts by typing… | Use |
|---|---|
| A city name | [`/places/dropdown/?search=`](#1-place-dropdown) |
| A postal code | [`/places/postal-codes/dropdown/?search=`](./postal_code_dropdown.md) — it ranks exact matches first |
| Nothing; they pick from lists | [Country](./country.md) → [Admin1](./admin1.md) → [Admin2](./admin2.md) → [Admin3](./admin3.md) → this |

Whichever path they take, finish with [Place Detail](#2-place-detail) to resolve the full chain.

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **The dropdown returns `id` / `cities` / `postal_code` instead of `label` / `value`.** | It is the only dropdown in the API with this shape; shared picker components need a special case. |
| 2 | **The place name key changes between endpoints** — `cities` in the dropdown, `name` in detail. | Easy to bind the wrong field. |
| 3 | **`postal_code` exact-match does not strip spaces**, though `search` does. | `?postal_code=560 001` fails where `?search=560 001` succeeds. |
| 4 | **No parameter is required**, so an unfiltered call scans the whole table. | The most common cause of a 408. |

## Related

- [Postal Code Dropdown](./postal_code_dropdown.md) — postal-code-first search over the same table
- [Country Dropdown](./country.md) — the hierarchy and shared conventions
- [Signup flow](./signup_flow.md#10-create-the-order) — where the resolved address is submitted
