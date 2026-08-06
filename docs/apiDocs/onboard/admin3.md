# Admin3 (Sub-district) Dropdown API Documentation

Third-level administrative divisions — sub-districts, talukas, tehsils — **for one district**.
Level 4 of the [geography hierarchy](./country.md#the-hierarchy), and the last before
[Place](./place.md).

- **Path**: `GET /api/v1/onboard/admin3/dropdown/`
- **Auth**: a **JWT** *or* a **temporary onboarding token**.
- **Shared behaviour**: pagination, timeout, and auth follow the
  [geography conventions](./country.md#geography-conventions) — **max page size 25**, **5-second
  timeout**.

> **This is the level most often absent.** Many countries have no Admin3 data at all, so a
> `NO_FURTHER_DATA` response here is routine rather than exceptional. Design the address form to
> skip straight to [Place](./place.md) when it appears.

## Request

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `admin2_id` * | UUID | The parent district, from [`/admin2/dropdown/`](./admin2.md). | **Required.** Omitting it → `400 VALIDATION_ERROR`. |
| `name` | string | **Exact** match on the sub-district name. | Case-sensitive, whole-value. Use `search` for partial matching. |
| `search` | string | Partial, case-insensitive match on `name`. | Substring, matched anywhere. |
| `page` | integer | Page number. Default `1`. | — |
| `page_size` | integer | Results per page. Default `10`. | **Capped at 25.** |

## Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Sub-districts found. Also the response when `name` or `search` matches none — `data` is then `[]`. | `success` | `null` |
| **400** | `admin2_id` not supplied. | `admin2_id parameter is required` | `{"code": "VALIDATION_ERROR"}` |
| **400** | The district has **no sub-districts at all**, or `admin2_id` matches nothing. | `No sub-districts found for this district` | `{"code": "NO_FURTHER_DATA"}` |
| **401** | No valid JWT or onboarding token. | `Authentication credentials were not provided.` | — |
| **408** | Query exceeded 5 seconds. | `Query timeout exceeded. Please refine your search parameters.` | `{"timeout": …}` |

**200 OK**
```json
{
  "status": true,
  "message": "success",
  "data": [
    { "value": "e4f5a6b7-…", "label": "Bengaluru North" }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

| Field | Type | Meaning |
|---|---|---|
| `value` | UUID | The sub-district id. Pass as `admin3_id` to [Place](./place.md). |
| `label` | string | The sub-district name, ready to display. |

---

## `NO_FURTHER_DATA`

Identical in shape to [Admin1's](./admin1.md#no_further_data--read-this-before-wiring-the-ui), but
**expect it far more often here** — Admin3 coverage is sparse in most datasets.

> **The right handling is to hide the field and move on.** Query
> [Place](./place.md) with `admin2_id` instead of `admin3_id`; the results are the same set of
> places, just not narrowed by a level that does not exist.

> **A fruitless `search` still returns `200` with `data: []`.** The emptiness check runs before
> filters, so only a genuinely childless parent produces the `400`.

---

## Notes

> **`admin2_id` is the district id.** Passing an `admin1_id` returns `NO_FURTHER_DATA`, which reads
> like missing data rather than a wrong parameter.

> **`name` is exact and case-sensitive.** Use `search` for user input.

## Related

- [Admin2 (District) Dropdown](./admin2.md) — where `admin2_id` comes from
- [Place Dropdown](./place.md) — the leaf level, and where the address actually resolves
- [Country Dropdown](./country.md) — the shared geography conventions
