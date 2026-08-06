# Admin2 (District) Dropdown API Documentation

Second-level administrative divisions — districts, counties — **for one state**. Level 3 of the
[geography hierarchy](./country.md#the-hierarchy).

- **Path**: `GET /api/v1/onboard/admin2/dropdown/`
- **Auth**: a **JWT** *or* a **temporary onboarding token**.
- **Shared behaviour**: pagination, timeout, and auth follow the
  [geography conventions](./country.md#geography-conventions) — **max page size 25**, **5-second
  timeout**.

> **`admin1_id` is required**, and it is the **state** id, not the country id.

## Request

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `admin1_id` * | UUID | The parent state, from [`/admin1/dropdown/`](./admin1.md). | **Required.** Omitting it → `400 VALIDATION_ERROR`. |
| `name` | string | **Exact** match on the district name. | Case-sensitive, whole-value. Use `search` for partial matching. |
| `search` | string | Partial, case-insensitive match on `name`. | Substring, matched anywhere. |
| `page` | integer | Page number. Default `1`. | — |
| `page_size` | integer | Results per page. Default `10`. | **Capped at 25.** |

## Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Districts found. Also the response when `name` or `search` matches none — `data` is then `[]`. | `success` | `null` |
| **400** | `admin1_id` not supplied. | `admin1_id parameter is required` | `{"code": "VALIDATION_ERROR"}` |
| **400** | The state has **no districts at all**, or `admin1_id` matches nothing. | `No districts found for this state` | `{"code": "NO_FURTHER_DATA"}` |
| **401** | No valid JWT or onboarding token. | `Authentication credentials were not provided.` | — |
| **408** | Query exceeded 5 seconds. | `Query timeout exceeded. Please refine your search parameters.` | `{"timeout": …}` |

**200 OK**
```json
{
  "status": true,
  "message": "success",
  "data": [
    { "value": "d3e4f5a6-…", "label": "Bengaluru Urban" }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

| Field | Type | Meaning |
|---|---|---|
| `value` | UUID | The district id. Pass as `admin2_id` to [Admin3](./admin3.md) and [Place](./place.md). |
| `label` | string | The district name, ready to display. |

---

## `NO_FURTHER_DATA`

Identical in shape and meaning to [Admin1's](./admin1.md#no_further_data--read-this-before-wiring-the-ui).

> **Treat it as "skip this level".** A state with no districts is normal in many datasets — move the
> form to [Place](./place.md) filtered by `admin1_id`, rather than blocking the user.

> **A fruitless `search` still returns `200` with `data: []`**, because the emptiness check runs
> before filters are applied. Branch on the `400`, not on an empty array.

---

## Notes

> **`admin1_id` is the state id, not the country id.** Passing a `country_id` here yields
> `NO_FURTHER_DATA`, which reads like "this state has no districts" and is easy to misdiagnose.

> **`name` is exact and case-sensitive.** Use `search` for user input.

## Related

- [Admin1 (State) Dropdown](./admin1.md) — where `admin1_id` comes from
- [Admin3 (Sub-district) Dropdown](./admin3.md) — the next level down
- [Country Dropdown](./country.md) — the shared geography conventions
