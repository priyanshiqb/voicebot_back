# Languages API Documentation

The language reference table. Anything in the system that carries a language preference — a
[user](../user/users.md), a tenant, a pending [signup](./signup_flow.md) — is a foreign key to a
row here.

**Base path:** `/api/v1/onboard/languages/`

Two things govern this API:

- **Reads need a login; writes need a superuser.** Unlike [plans](./plans.md), listing languages is
  **not** public — a signup form that offers a language picker must fetch it after authenticating,
  or hard-code the options.
- **`code` is the functional value.** The seeded default is `en-IN`, and it is what a pending
  [signup](./signup_flow.md#3-start-the-signup) falls back to when no language is chosen.

> **This table is a label store, not a translation system.** Selecting a language records a
> preference; it does not change what any API returns. Voice-side language handling lives on the
> [Agent](../core/agent.md) `languages` field, which is a free-text array and unrelated to these
> rows.

## Endpoints Summary

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `GET` | `/onboard/languages/` | List languages (paginated) | Any authenticated user |
| `GET` | `/onboard/languages/{id}/` | Retrieve one | Any authenticated user |
| `GET` | `/onboard/languages/dropdown/` | Label/value picker options | Any authenticated user |
| `POST` | `/onboard/languages/` | Create | Superuser |
| `PUT` / `PATCH` | `/onboard/languages/{id}/` | Update | Superuser |
| `DELETE` | `/onboard/languages/{id}/` | Soft-delete | Superuser |
| `POST` | `/onboard/languages/bulk_delete/` | Bulk soft-delete | Superuser |

---

## Endpoint Details

### 1. List Languages

- **Path**: `GET /api/v1/onboard/languages/`
- **Headers**: `Authorization: Bearer <access_token>`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Beyond the last page → **404**. |
| `page_size` | integer | Rows per page. Default `10`. | Capped at `100`. |
| `search` | string | Partial, case-insensitive match on `name` and `code`. | Words split on spaces and **AND**ed. |
| `ordering` | string | Sort. Default `name` (alphabetical). | Only `name`, `code`, `created_at`, optionally `-` prefixed. |
| `name` | string | Exact match. `name__icontains` is also available for partial matching. | — |
| `code` | string | Exact match on the language code, e.g. `en-IN`. | — |
| `is_active` | boolean | Exact match. | `true` / `false`. |
| `created_at__date` / `__gte` / `__lte` | date / datetime | Creation window. | — |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c",
        "name": "English (India)",
        "code": "en-IN",
        "is_active": true,
        "created_at": "2026-01-04T09:00:00Z"
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more languages. | `success` |
| **400** | Malformed date or non-boolean filter value. | filter error |
| **401** | Not authenticated. | `Authentication credentials were not provided.` |
| **404** | `page` beyond the last page. | `Invalid page.` |

> **Pass `?is_active=true` for anything user-facing.** Inactive languages are included by default.

---

### 2. Retrieve a Language

- **Path**: `GET /api/v1/onboard/languages/{id}/`
- **Success Response (200 OK)**: as §1, plus `updated_at`.

| Status | When | `message` |
|---|---|---|
| **200** | Found — **including inactive rows**. | `Retrieved successfully` |
| **401** | Not authenticated. | `Authentication credentials were not provided.` |
| **404** | No such language, or soft-deleted. | `Not found.` |

---

### 3. Create a Language

**Superuser only.**

- **Path**: `POST /api/v1/onboard/languages/`
- **Request Payload**:
  ```json
  { "name": "Hindi", "code": "hi-IN" }
  ```

### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `code` * | string | The language code, e.g. `en-IN`, `hi`. Conventionally BCP-47, but **not validated** — any string is accepted. | Required. Max **50**. **Unique across all languages**, including soft-deleted ones. |
| `name` * | string | Human-readable label, e.g. `English (India)`. | Required. Max **255**. **Not unique** — two rows may share a name. |
| `is_active` | boolean | Whether it may be offered. | Optional, default `true`. |

**Read-only:** `id`, `created_at`, `updated_at`.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Created. | `Created successfully` | `null` |
| **400** | `code` already used. | `language with this code already exists.` | `{"code": [...]}` |
| **400** | `code` or `name` missing, or over its length limit. | `This field is required.` | `{"<field>": [...]}` |
| **401** | Not authenticated. | `Authentication credentials were not provided.` | `{"detail": …}` |
| **403** | Authenticated but not a superuser. | permission denied | `{"detail": …}` |

> **`code` uniqueness ignores soft deletion.** A deleted language keeps its code reserved
> permanently, so `hi-IN` cannot be recreated once deleted. Deactivate rather than delete.

> **Nothing validates the code against a standard.** `xx-YY` and `not a language` are both accepted.
> If your client maps codes to flags or locale bundles, validate before creating.

---

### 4. Update / Delete

- **Paths**: `PUT` / `PATCH` / `DELETE /api/v1/onboard/languages/{id}/` — **superuser only**.

| Status | When | `message` |
|---|---|---|
| **200** | Updated or soft-deleted. | `Updated successfully` / `Deleted successfully` |
| **400** | Validation failed (update only), e.g. a duplicate `code`. | as §3 |
| **401** / **403** | As in §3. | — |
| **404** | No such language, or already deleted. | `Not found.` |

> **Deleting a language in use does not fail and does not cascade.** Users, tenants, and pending
> signups reference this table, but soft deletion is invisible to those foreign keys — the row
> disappears from pickers while existing references keep resolving. Prefer `is_active: false`.

> **Deleting the `en-IN` row breaks the signup default.** A pending registration falls back to the
> language whose code is `en-IN`; if that row is gone the lookup returns nothing and new
> registrations are created with no language at all. It fails silently.

---

### 5. Dropdown

- **Path**: `GET /api/v1/onboard/languages/dropdown/`
- **Query Parameters**: `limit` (≤ **1000**; over that → **400 `Limit cannot exceed 1000`**), plus all List filters and `search`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [ { "label": "English (India) (en-IN)", "value": "7bf26cf4-…" } ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

`label` is `"<name> (<code>)"`. Note that a name already containing parentheses —
`English (India)` — produces the doubled-up label shown above. Build your own from List (§1) if
that matters.

---

### 6. Bulk Delete

- **Path**: `POST /api/v1/onboard/languages/bulk_delete/` — **superuser only**.
- **Request Payload**: `{ "ids": ["7bf26cf4-…", "…"] }`

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Languages to soft-delete. | Non-empty list. Every id must exist and be undeleted. All-or-nothing. |

| Status | When | `message` |
|---|---|---|
| **200** | All soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id unknown or already deleted. | `Some ids are invalid` |
| **401** / **403** | As in §3. | — |

---

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **`code` is not validated against BCP-47 or any standard.** | Invalid codes reach clients that expect to map them to locales. |
| 2 | **`code` stays reserved after soft deletion.** | A deleted language's code can never be reused. |
| 3 | **Deleting the `en-IN` row silently removes the signup default.** | New registrations are created with no language, with no error surfaced. |
| 4 | **Deletion has no in-use guard.** | A language still referenced by users or tenants disappears from pickers without warning. |
| 5 | **Reads require authentication.** | A pre-login signup form cannot fetch the list and must hard-code its options. |

## Related

- [Timezones API](./timezones.md) — the sibling reference table
- [Signup flow](./signup_flow.md#3-start-the-signup) — where `language_id` is first set
- [Users API](../user/users.md) — per-user preference
