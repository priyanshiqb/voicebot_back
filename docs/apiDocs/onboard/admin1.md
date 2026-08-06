# Admin1 (State) Dropdown API Documentation

First-level administrative divisions — states, provinces, regions — **for one country**. Level 2 of
the [geography hierarchy](./country.md#the-hierarchy).

- **Path**: `GET /api/v1/onboard/admin1/dropdown/`
- **Auth**: a **JWT** *or* a **temporary onboarding token**.
- **Shared behaviour**: pagination, timeout, and auth follow the
  [geography conventions](./country.md#geography-conventions) — note the **max page size of 25** and
  the **5-second timeout**.

> **`country_id` is required.** There is no "all states everywhere" mode; omitting it is a `400`.

## Request

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `country_id` * | UUID | The parent country, from [`/countries/dropdown/`](./country.md). | **Required.** Omitting it → `400 VALIDATION_ERROR`. |
| `name` | string | **Exact** match on the state name. | Case-sensitive, whole-value. Use `search` for partial matching. |
| `search` | string | Partial, case-insensitive match on `name`. | Substring, matched anywhere. |
| `page` | integer | Page number. Default `1`. | — |
| `page_size` | integer | Results per page. Default `10`. | **Capped at 25.** |

## Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | States found. Also the response when `name` or `search` matches none of them — `data` is then `[]`. | `success` | `null` |
| **400** | `country_id` not supplied. | `country_id parameter is required` | `{"code": "VALIDATION_ERROR"}` |
| **400** | The country has **no states at all**, or `country_id` matches nothing. | `No states found for this country` | `{"code": "NO_FURTHER_DATA"}` |
| **401** | No valid JWT or onboarding token. | `Authentication credentials were not provided.` | — |
| **408** | Query exceeded 5 seconds. | `Query timeout exceeded. Please refine your search parameters.` | `{"timeout": …}` |

**200 OK**
```json
{
  "status": true,
  "message": "success",
  "data": [
    { "value": "c2d3e4f5-…", "label": "Karnataka" }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

| Field | Type | Meaning |
|---|---|---|
| `value` | UUID | The state id. Pass as `admin1_id` to [Admin2](./admin2.md) and [Place](./place.md). |
| `label` | string | The state name, ready to display. |

---

## `NO_FURTHER_DATA` — read this before wiring the UI

**An empty result is a `400`, not an empty `200`** — but only when the *parent* has no children at
all. The distinction matters:

| Situation | Status | `errors.code` |
|---|---|---|
| Country has no states in the dataset | **400** | `NO_FURTHER_DATA` |
| `country_id` is a valid UUID matching no country | **400** | `NO_FURTHER_DATA` |
| Country has states, but your `search` matched none | **200** | `null`, `data: []` |
| `country_id` omitted | **400** | `VALIDATION_ERROR` |

> **Treat `NO_FURTHER_DATA` as "skip this level", not as an error to show the user.** Some countries
> genuinely have no Admin1 rows; the correct response is to move the form straight to
> [Place](./place.md), filtered by `country_id` alone. Rendering "No states found for this country"
> as a validation failure blocks a legitimate address.

> **It cannot distinguish "no such country" from "country with no states".** Both return
> `NO_FURTHER_DATA`, so validate `country_id` against the country dropdown first if you need to
> tell them apart.

> **The check runs before `name` and `search` are applied**, which is why a fruitless search still
> returns `200`. Do not branch on `data.length === 0` to detect the end of the hierarchy — branch on
> the `400`.

---

## Notes

> **`name` is exact and case-sensitive.** `?name=karnataka` finds nothing. Use `search` for anything
> the user typed.

> **`country_id` is not validated as a UUID before use.** A malformed value produces a database
> error rather than a clean `400`. Pass ids straight from the country dropdown.

## Related

- [Country Dropdown](./country.md) — where `country_id` comes from, and the shared conventions
- [Admin2 (District) Dropdown](./admin2.md) — the next level down
- [Place Dropdown](./place.md) — the leaf level
