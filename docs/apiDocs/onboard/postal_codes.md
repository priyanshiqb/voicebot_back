# Onboarding Postal Codes API — ❌ REMOVED

> ⚠️ **This API no longer exists.** The standalone `PostalCode` model and its
> `/api/v1/onboard/postal-codes/` endpoints (list / create / retrieve / update / delete /
> dropdown / nearest) were **removed** (migration `0017`). There is no view, serializer, or
> route for it anymore. This page is kept only as a tombstone so old links don't 404.

## What to use instead

Postal-code lookup is now part of the **Place / geography** feature:

- **Postal-code dropdown & search** → [Geography - Postal Codes Dropdown](postal_code_dropdown.md)
  (`GET /api/v1/onboard/places/postal-codes/dropdown/`)
- **Places** → [Place](place.md)
- **Country / Admin1–3** → [country.md](country.md), [admin1.md](admin1.md), [admin2.md](admin2.md), [admin3.md](admin3.md)

*(Note: a few stale unit tests still import `onboard.views.postal_code.PostalCodeViewSet`; those
are a separate code-cleanup item, not part of the API docs.)*
