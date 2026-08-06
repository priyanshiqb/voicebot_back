# Postal Codes API — removed

**This API no longer exists.** The standalone `PostalCode` model and its
`/api/v1/onboard/postal-codes/` endpoints — list, create, retrieve, update, delete, dropdown, and
nearest — were removed in migration `0017`.

There is no model, view, serializer, or route for it. Every path under
`/api/v1/onboard/postal-codes/` returns **404**. This page exists so old links land somewhere
useful rather than on a dead end.

*(Verified against the codebase: no `PostalCode` model and no `PostalCodeViewSet` remain, and the
stale test imports an earlier version of this page warned about have since been cleaned up.)*

## What replaced it

Postal codes are no longer a table of their own. They are a **column on `Place`**, which pairs a
locality name with its code — so one code covering four localities is four `Place` rows, not one
postal-code row.

| You used to call | Call instead |
|---|---|
| `GET /onboard/postal-codes/dropdown/` | [`GET /onboard/places/postal-codes/dropdown/`](./postal_code_dropdown.md) |
| `GET /onboard/postal-codes/?search=` | [`GET /onboard/places/dropdown/?search=`](./place.md#1-place-dropdown) — searches name **and** code |
| `GET /onboard/postal-codes/{id}/` | [`GET /onboard/places/{id}/`](./place.md#2-place-detail) — returns the full parent chain |
| `GET /onboard/postal-codes/nearest/` | **No replacement.** Proximity search was dropped and has no equivalent. |
| `POST` / `PUT` / `DELETE /onboard/postal-codes/` | **No replacement.** Geography is reference data, loaded by the `load_postal_data` management command rather than through the API. |

## What changed for callers

> **Ids are not portable.** A `PostalCode` id from the old API has no counterpart in `Place`. Any
> stored id must be re-resolved by searching for the code.

> **A postal code no longer has one id.** It maps to as many `Place` rows as it has localities, so
> the user must pick which one — which is why the
> [postal-code dropdown](./postal_code_dropdown.md) labels each option `"<name> | <code>"`.

> **Geography is now read-only over HTTP.** The old write endpoints are gone; the dataset is loaded
> and refreshed by `python manage.py load_postal_data`.

## Related

- [Postal Code Dropdown](./postal_code_dropdown.md) — the direct replacement
- [Place API](./place.md) — the table postal codes now live on
- [Country Dropdown](./country.md) — the hierarchy and shared geography conventions
