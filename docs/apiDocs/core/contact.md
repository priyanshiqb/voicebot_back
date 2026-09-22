# Contacts API Documentation

The people this workspace can call or book. One row per person, and **identity is the normalized
E.164 phone number** — not the raw string someone typed. `+1 (415) 555-2671`, `+1-415-555-2671`,
and `+14155552671` are all the same contact, and the system will refuse to store them as three.

Four things to understand before you build against this:

- **`phone_e164` is derived, never submitted.** You send `phone_number` in whatever shape you have
  it; the server normalizes and stores both. A client-supplied `phone_e164` is ignored outright,
  because letting the identity key drift from the raw number defeats every duplicate check that
  depends on it.
- **Two things are unique per tenant, both only among live rows:** the phone number, and the
  name. Soft-deleting a contact releases both for reuse.
- **Bulk creation does not happen here.** `POST /contacts/import/` deliberately returns **405** —
  use the staged [Contact Import](./contact_import.md) flow, which normalizes and shows you
  conflicts before anything is written.
- **This module records consent; it does not enforce it.** Nothing on this API blocks a call to a
  `do_not_call` contact. The dialler is responsible for checking.

**Base path:** `/api/v1/core/contacts/`

> **VB_AGENT access:** none. This viewset declares no `vb_agent_allowed_actions`, so every action —
> including `lookup` — returns **403 `This endpoint is not available to agent tokens.`** for a
> machine caller. Note that `lookup` is *described* in the OpenAPI schema as being for
> inbound-caller resolution by the voice runtime; that is its intended purpose, but the runtime
> cannot currently reach it with an agent token. Call it with a user JWT, or have the endpoint
> opted in first. See [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md).

## Endpoints Summary
- `GET /api/v1/core/contacts/` - List contacts (paginated, searchable, filterable)
- `GET /api/v1/core/contacts/{id}/` - Retrieve one contact
- `POST /api/v1/core/contacts/` - Create a contact
- `PUT` / `PATCH /api/v1/core/contacts/{id}/` - Update a contact
- `DELETE /api/v1/core/contacts/{id}/` - Soft-delete a contact (frees its number)
- `GET /api/v1/core/contacts/lookup/` - Find a contact by phone number
- `GET /api/v1/core/contacts/tags/` - Distinct tags in use, with counts
- `GET /api/v1/core/contacts/export/` - Download contacts as CSV or XLSX
- `GET /api/v1/core/contacts/dropdown/` - Get label/value list
- `POST /api/v1/core/contacts/bulk_delete/` - Bulk soft-delete
- `POST /api/v1/core/contacts/import/` - **Disabled**, returns 405

---

## Phone numbers — read this first

| Field | Writable | Meaning |
|---|---|---|
| `phone_number` | **yes** | The number exactly as supplied. Preserved for display, never used for matching. |
| `phone_e164` | no | Normalized identity key, e.g. `+14155552671`. Re-derived on every save that touches `phone_number`. |
| `phone_region` | no | ISO-3166 alpha-2 region resolved during normalization, e.g. `US`. |

**How a number is parsed:**

1. If it starts with `+`, it is treated as international and parsed on its own — no region needed.
2. Otherwise it is parsed against `CONTACT_DEFAULT_PHONE_REGION` (default **`IN`**, India).
3. If neither applies — the number is national and no default region is configured — the request
   is **rejected**. The country is never guessed, because guessing silently rewrites the number to
   the wrong country, which is worse than refusing it.
4. The parsed result must be a *valid* number for its region, not merely well-formed. `+1999` is
   rejected even though it parses.

> **Create and update cannot override the region.** There is no `region` field on this API — the
> parse uses the deployment default and nothing else. The only endpoint that accepts a per-request
> `region` is [`lookup`](#6-look-up-a-contact-by-phone-number). If you serve multiple countries and
> your callers submit national-format numbers, require international format client-side.

**Uniqueness** is `(tenant, phone_e164)` among non-deleted rows. Two live contacts in the same
tenant can never share a number; a soft-deleted one does not block reuse.

---

## Name uniqueness

`(tenant, lower(first_name), lower(last_name))` is unique among non-deleted rows (BUG-001234).
Two live contacts in the same tenant cannot share a name.

- **Case-insensitive, and whitespace is trimmed first.** `John Smith`, `john smith` and
  `  John  ` / `Smith` are all the same name.
- **Both halves count together.** `John Smith` does not collide with `John Doe`.
- **Wholly unnamed contacts are exempt**, and there may be any number of them. A name is
  optional here and contacts captured from an inbound call arrive without one, so a rule that
  made `("", "")` unique would let exactly one such contact exist.
- **Soft-deleted contacts do not hold their name**, the same way they do not hold their number.
  Restoring one through the admin fails if the name has been taken since.

A duplicate is a **400** on `first_name`, not a 500 — the check runs in the serializer and the
partial unique index behind it catches anything that races past.

---

## Consent & suppression

| Field | Type | Meaning |
|---|---|---|
| `consent_status` | choice | `UNKNOWN` (default) / `GRANTED` / `DENIED` / `WITHDRAWN` |
| `consent_source` | string | Free text describing where consent was captured, e.g. `web-form`. |
| `consent_at` | datetime | When it was captured. |
| `do_not_call` | boolean | Hard suppression flag. Indexed, so filtering on it is cheap. |
| `opted_out_at` | datetime | When the contact opted out. |

All five are writable through this API and the admin, and are **never** writable by CSV import —
re-uploading a stale list must not resurrect someone who opted out.

> **Recording ≠ enforcing.** Setting `do_not_call: true` does not stop a campaign from dialling
> this person. It records the fact for the dialler to honour.

---

## Endpoint Details

### 1. List Contacts

Paginated, tenant-scoped list. The list payload is deliberately lighter than the detail
payload — it omits `notes`, `phone_region`, and the consent detail fields.

- **Path**: `GET /api/v1/core/contacts/`
- **Headers**: `Authorization: Bearer <token>`
- **Query Parameters**:

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `page` | integer | Page number. Default `1`. | Beyond the last page → **404**. |
| `page_size` | integer | Rows per page. Default `10`. | Capped at `100`. |
| `search` | string | Partial, case-insensitive match across `first_name`, `last_name`, `phone_e164`, `phone_number`, `email_address`, `company`. | Words are split on spaces and **AND**ed; each word must match at least one field. |
| `ordering` | string | Sort. Default `-created_at`. | `name` (first + last, case-insensitive — same as `full_name`), `phone_number` (by the normalised E.164 number, then as typed), `is_active` (inactive first ascending), `created_at`, `updated_at`, `first_name`, `last_name`, `full_name`, `phone_e164`, `email_address`, `company`, `consent_status` — each optionally `-` prefixed. Comma-separate for multiple keys. Ties are broken by newest first. Anything else is ignored silently. |
| `is_active` | boolean | Exact match. | `true` / `false`. |
| `do_not_call` | boolean | Exact match. Use to build a suppression list. | `true` / `false`. |
| `consent_status` | string | Exact match. | One of the four choices; unknown value → **400**. |
| `source` | string | Exact match on how the contact entered the system. | One of `MANUAL`, `IMPORT`, `API`, `CALL`. |
| `company` | string | **Exact, case-sensitive** match on the whole field. | `?company=Acme` will not match `acme corp`. Use `search` for partial matching. |
| `tags` | string | Comma-separated. Matches contacts carrying **ANY** of these tags. | Blank entries are dropped; an all-blank value is a no-op, not an error. |
| `tags_all` | string | Comma-separated. Matches contacts carrying **ALL** of them. | Same. |

> **Every sort is tie-broken by `-created_at` then `id`.** `company` and
> `consent_status` have a handful of distinct values across a whole contact book, so
> sorting on either leaves almost every row tied — and SQL makes no promise about the
> order of tied rows between two queries. Without the tiebreaker, page 2 can repeat a
> row from page 1 and skip another.

> **Searching by phone: use the digits.** `search` splits on spaces, so `?search=+1 (415)` becomes
> two terms that must *both* match. Search `4155552671` or `+14155552671` instead — `phone_e164`
> and `phone_number` are both covered.

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "success",
    "data": [
      {
        "id": "0f0c2a2e-2a1e-4f5a-9a41-9d1f1a5a6b21",
        "tenant": { "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c", "name": "Alpha Corp", "subdomain": "alpha-corp" },
        "first_name": "Ada",
        "last_name": "Lovelace",
        "full_name": "Ada Lovelace",
        "phone_number": "+1 (415) 555-2671",
        "phone_e164": "+14155552671",
        "email_address": "ada@example.com",
        "company": "Analytical Engines",
        "tags": ["vip", "lead"],
        "source": "MANUAL",
        "consent_status": "GRANTED",
        "do_not_call": false,
        "is_active": true,
        "created_at": "2026-07-29T09:12:00Z",
        "updated_at": "2026-07-29T09:12:00Z",
        "created_by": { "id": "5fa85f64-…", "email": "alex@acme.com" }
      }
    ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more rows. An empty list is a normal result, not an error. | `success` |
| **400** | Unknown `consent_status` / `source` value, or a non-boolean where a boolean is expected. | filter error |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping, or an agent token. | `Your user account is not associated with any active tenant.` / `This endpoint is not available to agent tokens.` |
| **404** | `page` beyond the last page. | `Invalid page.` |

---

### 2. Create Contact

- **Path**: `POST /api/v1/core/contacts/`
- **Headers**: `Authorization: Bearer <token>`
- **Request Payload**:
  ```json
  {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "phone_number": "+14155552671",
    "email_address": "ada@example.com",
    "company": "Analytical Engines",
    "tags": ["vip", "lead"],
    "notes": "Met at the London conference"
  }
  ```

### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `phone_number` * | string | The person's number, in any shape you have it. This is the only required field — a contact with no name is legal, one with no number is not. | Max **32** characters. Must normalize to a valid E.164 number (see [Phone numbers](#phone-numbers--read-this-first)). Must not already belong to a live contact in this tenant. |
| `first_name` | string | Given name. | Optional, may be blank. Max **120**. Trimmed. Together with `last_name`, must not match a live contact in this tenant — see [Name uniqueness](#name-uniqueness). |
| `last_name` | string | Family name. | Optional, may be blank. Max **120**. Trimmed. See `first_name`. |
| `email_address` | string | Email. Not unique — two contacts may share one. | Optional. Must be a valid email address. Max **254**. |
| `company` | string | Organisation name. Used by the exact-match `company` filter, so keep it consistent if you intend to filter on it. | Optional. Max **200**. |
| `tags` | string array | Free-form labels for segmentation. | Optional, default `[]`. At most **25** entries (`CONTACT_MAX_TAGS_PER_CONTACT`). Each tag max **50** characters. Entries are trimmed; blank ones are dropped; duplicates are removed, keeping first-seen order. |
| `notes` | string | Free text. No length limit. | Optional. |
| `source` | choice | How this contact entered the system. | Optional, default `MANUAL`. One of `MANUAL`, `IMPORT`, `API`, `CALL`. Writable — the server does not overwrite it, so set it honestly. |
| `consent_status` | choice | See [Consent & suppression](#consent--suppression). | Optional, default `UNKNOWN`. |
| `consent_source` | string | Where consent was captured. | Optional. Max **200**. |
| `consent_at` | datetime | When consent was captured. ISO-8601. | Optional, nullable. Not cross-checked against `consent_status` — you can record a date with `UNKNOWN`. |
| `do_not_call` | boolean | Hard suppression. | Optional, default `false`. |
| `opted_out_at` | datetime | When they opted out. ISO-8601. | Optional, nullable. Not cross-checked against `do_not_call`. |
| `is_active` | boolean | Soft on/off switch, independent of deletion. | Optional, default `true`. |

**Read-only, ignored if you send them:** `id`, `tenant`, `phone_e164`, `phone_region`, `full_name`,
`last_contacted_at`, `created_at`, `updated_at`, `deleted_at`, `is_deleted`, `created_by`,
`updated_by`, `deleted_by`.

> **The tag limit is checked before cleanup.** The count check runs against the raw list you sent,
> then blanks and duplicates are removed. So 26 tags where one is `""` is a **400**, even though
> only 25 would have survived. De-duplicate before posting if you are near the limit.

> **`full_name` is computed, not stored.** It is `first_name` + `last_name` joined with a space and
> trimmed, so a contact with neither returns `""`.

- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "Created successfully",
    "data": {
      "id": "0f0c2a2e-2a1e-4f5a-9a41-9d1f1a5a6b21",
      "tenant": { "id": "7bf26cf4-e53b-48bb-bb82-261ee976378c", "name": "Alpha Corp", "subdomain": "alpha-corp" },
      "first_name": "Ada",
      "last_name": "Lovelace",
      "full_name": "Ada Lovelace",
      "phone_number": "+14155552671",
      "phone_e164": "+14155552671",
      "phone_region": "US",
      "email_address": "ada@example.com",
      "company": "Analytical Engines",
      "tags": ["vip", "lead"],
      "notes": "Met at the London conference",
      "source": "MANUAL",
      "consent_status": "UNKNOWN",
      "consent_source": "",
      "consent_at": null,
      "do_not_call": false,
      "opted_out_at": null,
      "last_contacted_at": null,
      "is_active": true,
      "is_deleted": false,
      "created_at": "2026-07-29T09:12:00Z",
      "updated_at": "2026-07-29T09:12:00Z",
      "deleted_at": null,
      "created_by": { "id": "5fa85f64-…", "email": "alex@acme.com" },
      "updated_by": { "id": "5fa85f64-…", "email": "alex@acme.com" },
      "deleted_by": null
    },
    "errors": null
  }
  ```

> **`tenant`, `created_by`, `updated_by`, `deleted_by` read back as objects, not ids** — and return
> `null` if the referenced row has been soft-deleted. Guard against nulls when rendering an
> audit trail.

### Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Contact created. | `Created successfully` | `null` |
| **400** | `phone_number` omitted or empty. | `Phone number is required.` | `{"phone_number": [...]}` |
| **400** | National-format number and no default region configured. | `Phone number must be in international format (e.g. +14155552671), or a default region must be configured.` | `{"phone_number": [...]}` |
| **400** | The configured default region is not a real ISO-3166 code. | `'XX' is not a recognised region code.` | `{"phone_number": [...]}` |
| **400** | Number parses but is not valid for its country. | `'<value>' is not a valid phone number.` | `{"phone_number": [...]}` |
| **400** | A live contact in this tenant already has this number. | `A contact with this phone number already exists.` | `{"phone_number": [...]}` |
| **400** | A live contact in this tenant already has this name. | `A contact with this name already exists.` | `{"first_name": [...]}` |
| **400** | More than 25 tags. | `A contact may have at most 25 tags.` | `{"tags": [...]}` |
| **400** | A tag longer than 50 characters. | `Each tag may be at most 50 characters.` | `{"tags": [...]}` |
| **400** | Invalid `email_address`, unknown `source` / `consent_status`, or a field over its max length. | field message | `{"<field>": [...]}` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` | `{"detail": ...}` |
| **403** | No active tenant mapping, or an agent token. | as above | `{"detail": ...}` |

The duplicate case returns a clean field error rather than a 500:
```json
{
  "status": false,
  "message": "A contact with this phone number already exists.",
  "data": null,
  "errors": { "phone_number": ["A contact with this phone number already exists."] }
}
```

> **The duplicate check is a pre-flight, not a lock.** Two simultaneous creates of the same number
> can both pass validation and race at the database constraint; the loser surfaces as a **500**.
> Retrying returns the clean `400` above, because by then the row exists. Retry once before
> reporting the error.

> **Superusers cannot create contacts here.** Tenant injection is skipped for superusers, and
> `tenant` is a required column, so the insert fails. Create contacts as a tenant-mapped user.

---

### 3. Retrieve a Contact

- **Path**: `GET /api/v1/core/contacts/{id}/`
- **Success Response (200 OK)**: the full object shape from §2, with `"message": "Retrieved successfully"`.

| Status | When | `message` |
|---|---|---|
| **200** | Found. | `Retrieved successfully` |
| **401** | Missing or invalid token. | `Authentication credentials were not provided.` |
| **403** | No tenant mapping, or an agent token. | as above |
| **404** | No such id, it is soft-deleted, or it belongs to another tenant. | — |

---

### 4. Update a Contact

- **Path**: `PUT` (full) or `PATCH` (partial) `/api/v1/core/contacts/{id}/`
- **Request Payload**: any subset of the writable fields in §2. `PUT` requires `phone_number`;
  `PATCH` does not.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Updated. | `Updated successfully` | `null` |
| **400** | Any validation failure from §2. | as above | `{"<field>": [...]}` |
| **400** | The new number already belongs to **another** live contact in this tenant. | `A contact with this phone number already exists.` | `{"phone_number": [...]}` |
| **400** | The new name already belongs to **another** live contact in this tenant. | `A contact with this name already exists.` | `{"first_name": [...]}` |
| **401** / **403** / **404** | As in §3. | — | — |

> **Changing `phone_number` changes identity.** `phone_e164` and `phone_region` are re-derived on
> save, so the contact now matches a different inbound caller and a different import row. The
> uniqueness check excludes the contact being edited, so re-submitting the number it already has is
> always fine.

> **`last_contacted_at` cannot be set here.** It is maintained by the voice runtime.

---

### 5. Delete a Contact

- **Path**: `DELETE /api/v1/core/contacts/{id}/`
- **Success Response (200 OK)**: `{ "status": true, "message": "Deleted successfully", "data": null, "errors": null }`

| Status | When | `message` |
|---|---|---|
| **200** | Soft-deleted. | `Deleted successfully` |
| **401** / **403** / **404** | As in §3. | — |

**Soft delete.** The row survives with `is_deleted: true` and drops out of every list, lookup, and
export. Two consequences:

- **The phone number is freed.** Uniqueness only covers live rows, so you (or an import) can create
  a new contact with the same number immediately afterwards. It will be a **different** contact
  with a new id and no history.
- **Call and booking history survives.** `AgentCallLog.contact` and `BookingParticipant.contact`
  both use `SET_NULL`, so those records lose the link but keep their own data.

> **Restoring is not exposed on this API.** A contact restored through the admin refuses to come
> back if its number has since been taken by another live contact — you get
> `Another active contact already uses this phone number. Change or remove that contact before
> restoring this one.` rather than a database error.

---

### 6. Look up a Contact by Phone Number

Normalizes the supplied number and returns the single matching contact. Built for inbound-caller
resolution: the caller ID arrives in whatever format the telephony provider uses, and this resolves
it to a person.

- **Path**: `GET /api/v1/core/contacts/lookup/`
- **Headers**: `Authorization: Bearer <token>`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `phone` * | string | The number to resolve, in any format. | Required. Must normalize to a valid E.164 number. |
| `region` | string | ISO-3166 alpha-2 code used to parse a **non-international** number, e.g. `US`. Ignored when `phone` starts with `+`. | Optional. Falls back to `CONTACT_DEFAULT_PHONE_REGION` when omitted. An unrecognised code → **400**. |

- **Success Response (200 OK)**: the full contact object from §2, with `"message": "Retrieved successfully"`.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Exactly one live contact in this tenant has that number. | `Retrieved successfully` | `null` |
| **400** | `phone` missing. | `This field is required.` | `null` |
| **400** | `phone` unparseable, invalid, or national-format with no usable region. | the normalization message, e.g. `'0000' is not a valid phone number.` | `null` |
| **404** | Normalized fine, but no live contact in this tenant has that number. | `No contact found for that phone number.` | `null` |
| **401** / **403** | As elsewhere. | — | — |

> **Lookup errors put the text in `message` and leave `errors` as `null`** — unlike create and
> update, which return a per-field `errors` object. Read `message` here.

> **This is the only endpoint that accepts `region`.** Create and update always use the deployment
> default. If a number resolves here but is rejected on create, a mismatched region is the likely
> reason.

---

### 7. Tags in Use

Every distinct tag across this tenant's contacts, with how many contacts carry it. Useful for
building a tag filter UI without asking the user to remember what exists.

- **Path**: `GET /api/v1/core/contacts/tags/`
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Retrieved successfully",
    "data": [ { "tag": "vip", "count": 42 }, { "tag": "lead", "count": 17 } ],
    "errors": null
  }
  ```

Sorted by `count` descending, then alphabetically (case-insensitive) for ties.

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more tags. `data: []` when no contact has any tag. | `Retrieved successfully` |
| **401** / **403** | As elsewhere. | — |

> **Not paginated, and counted in application code.** Every tagged contact in the tenant is read
> and tallied per request. It is fine at typical contact volumes but is not a cheap call — cache it
> client-side rather than refetching on every keystroke.

> **Tags are case-sensitive as data.** `VIP` and `vip` are two separate entries with separate
> counts; only the tie-break ordering is case-insensitive. Normalize casing when you write tags if
> you do not want both.

---

### 8. Export

- **Path**: `GET /api/v1/core/contacts/export/`
- **Headers**: `Authorization: Bearer <token>`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `file_format` | string | `csv` (default) or `xlsx`. | Any other value falls back to CSV rather than erroring. |

Every List filter and `search` term also applies here, so you can export a segment rather than the
whole book: `?do_not_call=true&file_format=xlsx`.

**Columns:** `first_name`, `last_name`, `phone_number`, `email_address`, `company`, `tags`,
`notes`, `source`, `consent_status`, `do_not_call`, `is_active`, `created_at`.

> **`id` and `phone_e164` are not exported** (BUG-001207). `id` is an internal UUID, and
> `phone_e164` is the normalized twin of `phone_number` — it put the same number in the file
> twice. Import still accepts an `id` column to match an existing row, so a file exported before
> this change re-imports unchanged.

| Status | When | Body |
|---|---|---|
| **200** | File returned. | Raw file bytes — **not** the JSON envelope. `Content-Disposition: attachment; filename="contact.csv"` (or `.xlsx`). |
| **401** | Missing or invalid token. | JSON error |
| **403** | No active tenant mapping, or an agent token. | JSON error |

> **Use `file_format`, not `format`.** DRF reserves `format` for content negotiation and rejects
> `?format=csv` with a **404** before this endpoint ever runs.

> **Formula injection is neutralized.** Cells beginning with `=`, `+`, `-`, `@`, tab, or carriage
> return are prefixed with a single quote, so a contact named `=HYPERLINK(...)` cannot execute when
> the file is opened in Excel. The visible text is unchanged.

---

### 9. Dropdown

- **Path**: `GET /api/v1/core/contacts/dropdown/`
- **Query Parameters**: `limit` (optional, ≤ **1000**; larger → **400 `Limit cannot exceed 1000`**), plus all the List filters and `search`.
- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Dropdown options retrieved successfully",
    "data": [ { "label": "Ada Lovelace (+14155552671)", "value": "0f0c2a2e-…" } ],
    "errors": null,
    "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
  }
  ```

The `label` is `"Full Name (+E164)"`, falling back to the bare `+E164` for a contact with no name —
so unnamed contacts are still distinguishable, which is the point of including the number.

---

### 10. Bulk Delete

- **Path**: `POST /api/v1/core/contacts/bulk_delete/`
- **Request Payload**: `{ "ids": ["0f0c2a2e-…", "…"] }`

| Field | Type | Description | Validation |
|---|---|---|---|
| `ids` * | UUID array | Contacts to soft-delete. | Non-empty list. Every id must exist, be undeleted, and be in your tenant. All-or-nothing. |

| Status | When | `message` |
|---|---|---|
| **200** | All soft-deleted; their numbers are now free for reuse. | `Bulk deleted successfully` |
| **400** | `ids` missing, not a list, or empty. | `ids must be a non-empty list` |
| **400** | Any id unknown, already deleted, or in another tenant — **nothing is deleted**. | `Some ids are invalid` |
| **401** / **403** | As elsewhere. | — |

> **Duplicate ids fail the whole request.** The check compares matched rows against the length of
> `ids`, so the same id twice looks like one missing row. De-duplicate client-side.

---

### 11. Import (disabled)

- **Path**: `POST /api/v1/core/contacts/import/`
- **Response (405 Method Not Allowed)**:
  ```json
  {
    "status": false,
    "message": "Direct import is not supported for contacts. Use the staged import flow at /api/v1/core/contact-imports/.",
    "data": null,
    "errors": null
  }
  ```

The route exists purely to return this message. A direct CSV import would bypass phone
normalization and conflict review, so bulk creation goes through
[Contact Import](./contact_import.md) instead — upload, review conflicts, then commit.

---

## Linked records

`AgentCallLog.contact` and `BookingParticipant.contact` are resolved automatically from their phone
numbers when those records are written — you do not link them by hand. Both use `SET_NULL`, so
deleting a contact never destroys call or booking history.

To link records created before this module existed:

```bash
python manage.py backfill_contact_links [--tenant-id <uuid>] [--batch-size 500]
```

Safe to re-run.

## Configuration

| Setting | Default | Effect |
|---|---|---|
| `CONTACT_DEFAULT_PHONE_REGION` | `IN` | Region used to parse numbers not in international form. Set per deployment to the market you serve. |
| `CONTACT_MAX_TAGS_PER_CONTACT` | `25` | Maximum tags on one contact. |

## Related

- [Contact Import API](./contact_import.md) — the supported path for bulk creation
- [Campaign API](./campaign.md) — how contacts become call lists
- [Agent Call Log API](./agent_call_log.md) — where `contact` links appear
- [Tenant scoping for VB_AGENT callers](./vb_agent_tenant_scoping.md)
