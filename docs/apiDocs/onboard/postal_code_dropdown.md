# Postal Code Dropdown API Documentation

Postal-code-first search over the same table as [Place](./place.md), tuned for the address form
where the user types a code rather than a city name. **Exact matches are ranked first.**

- **Path**: `GET /api/v1/onboard/places/postal-codes/dropdown/`
- **Auth**: a **JWT** *or* a **temporary onboarding token**.
- **Shared behaviour**: pagination, timeout, and auth follow the
  [geography conventions](./country.md#geography-conventions) — **max page size 25**, **5-second
  timeout**.

> **This is not a table of unique postal codes.** It returns *places*, one row per
> (name, postal code) pair — so a code covering four localities yields four rows. The label
> disambiguates them.

## Request

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `search` | string | Postal code **prefix**, spaces ignored on both sides. Typing `560` matches `560001`, `560002`, …; `560 001` matches `560001`. | Optional. Prefix match — it will not find a code by its middle or end. |
| `postal_code` | string | **Exact** match, spaces **not** stripped. | Optional. `560 001` will not match a stored `560001` — prefer `search`. |
| `country_id` | UUID | Narrow to one country. | Optional. |
| `admin1_id` | UUID | Narrow to one state. | Optional. |
| `admin2_id` | UUID | Narrow to one district. | Optional. |
| `admin3_id` | UUID | Narrow to one sub-district. | Optional. |
| `page` | integer | Page number. Default `1`. | — |
| `page_size` | integer | Results per page. Default `10`. | **Capped at 25.** |

## Ordering

| With `search` | Without `search` |
|---|---|
| Exact matches first, then prefix matches; within each group by `postal_code`, then `name`. | By `postal_code`, then `name`. |

> **The exact-match-first ranking is the point of this endpoint.** A user typing a complete code
> sees the precise locality at the top, with neighbouring codes below — rather than an alphabetical
> list where the exact hit is buried.

## Responses

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more places. An empty `data` array is normal — **there is no `NO_FURTHER_DATA` here**. | `success` |
| **401** | No valid JWT or onboarding token. | `Authentication credentials were not provided.` |
| **408** | Query exceeded 5 seconds. | `Query timeout exceeded. Please refine your search parameters.` |

**200 OK**
```json
{
  "status": true,
  "message": "success",
  "data": [
    { "value": "f5a6b7c8-…", "label": "Bengaluru GPO | 560001" },
    { "value": "a6b7c8d9-…", "label": "Vidhana Soudha | 560001" }
  ],
  "errors": null,
  "meta": { "count": 2, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

| Field | Type | Meaning |
|---|---|---|
| `value` | UUID | The **place** id — pass it to [Place Detail](./place.md#2-place-detail) to resolve the full address. |
| `label` | string | `"<place name> \| <postal code>"`, so duplicated codes stay distinguishable. |

> **Unlike the [place dropdown](./place.md#1-place-dropdown), this one does use `label` / `value`**
> and binds to a standard picker component.

> **`value` is a place id, not a postal code.** Storing the label's numeric part and looking it up
> later will not work; keep the id.

---

## Notes

> **`search` strips spaces; `postal_code` does not.** For anything the user typed, use `search`.
> Reserve `postal_code` for a value you already hold in canonical form.

> **Prefix matching only.** `?search=001` will not find `560001`. If your market's codes are
> commonly written with a prefix the user omits, normalise before sending.

> **Always send a parent filter or a `search` term.** An unfiltered call scans millions of rows and
> is the usual cause of a 408.

> **Pair with `country_id` when you serve several markets.** Postal-code formats overlap between
> countries, so an unscoped `search` can return matches from the wrong one.

## Related

- [Place API](./place.md) — city-name search, and the detail endpoint that resolves the full chain
- [Country Dropdown](./country.md) — the hierarchy and shared conventions
- [Signup flow](./signup_flow.md#10-create-the-order) — where the address is submitted
