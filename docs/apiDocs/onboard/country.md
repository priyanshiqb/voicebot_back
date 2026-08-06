# Country Dropdown API Documentation

The top of the geography hierarchy. Feeds the country picker on the
[billing address form](./signup_flow.md#10-create-the-order), and supplies the dial code and phone
format mask a phone input needs.

- **Path**: `GET /api/v1/onboard/countries/dropdown/`
- **Auth**: a **JWT** *or* a **temporary onboarding token** — see
  [Geography conventions](#geography-conventions).

## The hierarchy

```
Country  →  Admin1 (state)  →  Admin2 (district)  →  Admin3 (sub-district)  →  Place (city + postal code)
```

Each level filters the next by the parent's id. Start here, then
[Admin1](./admin1.md) → [Admin2](./admin2.md) → [Admin3](./admin3.md) → [Place](./place.md).

## Geography conventions

These apply to **every** geography endpoint, and differ from the rest of the API.

| Aspect | Behaviour |
|---|---|
| **Auth** | `CombinedOnboardingAuthentication` — a normal JWT **or** a temporary onboarding token. Deliberate: the address form runs mid-signup, before a real account exists. |
| **Pagination** | `page` / `page_size`, default **10**, **max 25** — not 100 as elsewhere. Larger values silently clamp. |
| **Query timeout** | A **5-second** PostgreSQL statement timeout. Exceeding it returns **408**, not a 500. |
| **No `limit` parameter** | Unlike other dropdowns, these paginate rather than take `limit`. |
| **Not soft-delete aware** | Geography tables are reference data with no `is_deleted` column. |

**408 Request Timeout** — the shared shape:
```json
{
  "status": false,
  "message": "Query timeout exceeded. Please refine your search parameters.",
  "data": null,
  "errors": { "timeout": "The requested data took too long to fetch." }
}
```

> **A 408 means your query was too broad, not that the server is down.** Add a parent filter or a
> `search` term and retry.

---

## Request

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `code` | string | Exact match on the ISO country code, e.g. `IN`. | Case-sensitive. |
| `name` | string | **Exact** match on the country name. | Case-sensitive, whole-value. For partial matching use `search`. |
| `search` | string | Partial, case-insensitive match on `name`. | Substring, matched anywhere in the name. |
| `page` | integer | Page number. Default `1`. | — |
| `page_size` | integer | Results per page. Default `10`. | **Capped at 25.** |

No parameter is required — calling with none returns the first page of every country.

## Responses

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more countries. An empty `data` array is a normal result when a filter matches nothing. | `success` |
| **401** | No valid JWT or onboarding token. | `Authentication credentials were not provided.` |
| **408** | The query took longer than 5 seconds. | `Query timeout exceeded. Please refine your search parameters.` |

**200 OK**
```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "value": "b1c2d3e4-…",
      "label": "India",
      "dial_code": "+91",
      "format_mask": "XXXXX XXXXX"
    }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

### Response fields

| Field | Type | Meaning |
|---|---|---|
| `value` | UUID | The country id. Pass as `country_id` to [Admin1](./admin1.md) and [Place](./place.md). |
| `label` | string | The country name, ready to display. |
| `dial_code` | string \| null | International dialling prefix, e.g. `+91`. **Nullable** — not every row has one. |
| `format_mask` | string \| null | Hint for formatting a phone input, e.g. `XXXXX XXXXX`. **Nullable.** |

> **`dial_code` and `format_mask` are why this dropdown carries extra fields.** Selecting a country
> can drive the phone input directly, with no second lookup. Both are nullable, so guard before
> using them.

> **Unlike [Admin1–3](./admin1.md), an empty result here is a `200`, not a `400`.** The
> `NO_FURTHER_DATA` convention applies only to the levels that require a parent id.

---

## Notes

> **`name` is an exact, case-sensitive match.** `?name=india` finds nothing; `?name=India` works.
> Use `?search=india` for anything the user typed.

> **`page_size` caps at 25.** With roughly 250 countries, expect at least ten pages — or filter with
> `search` and let the user type.

> **This endpoint is reachable mid-signup.** A temporary onboarding token authenticates it, which is
> what lets the billing-address step populate its country list before the user has an account.

## Related

- [Admin1 (State) Dropdown](./admin1.md) — the next level down
- [Place Dropdown](./place.md) — cities and postal codes
- [Signup flow](./signup_flow.md#10-create-the-order) — where the address form is submitted
