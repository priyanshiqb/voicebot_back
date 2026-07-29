# Contacts API

## Overview
Tenant-scoped people this workspace can call or book. Every contact is identified by its
**normalized E.164 phone number**, not by the raw string that was typed — so
`+1 (415) 555-2671`, `+1-415-555-2671`, and `+14155552671` are all the same contact.

**Base Path:** `/api/v1/core/contacts/`

> Bulk creation is **not** available here. Use the staged
> [Contact Import](contact_import.md) flow, which adds conflict review before anything
> is written.

---

## Phone numbers

| Field | Writable | Meaning |
|---|---|---|
| `phone_number` | yes | The number exactly as supplied. Display only. |
| `phone_e164` | **no** | Normalized identity key. Derived on every save. |
| `phone_region` | **no** | ISO-3166 alpha-2 region resolved during normalization. |

Numbers in international form (`+…`) always work and parse to their own country. A number
in national form (`98765 43210`) is parsed using `CONTACT_DEFAULT_PHONE_REGION`, which
defaults to **`IN`** (India). Override it per environment for deployments serving another
market.

If no region resolves and the number is not international, the request is rejected — the
number is never guessed, because guessing silently rewrites it to the wrong country.

**Uniqueness** is `(tenant, phone_e164)` and applies only to live contacts. Soft-deleting
a contact frees its number for reuse.

---

## Consent & suppression

| Field | Notes |
|---|---|
| `consent_status` | `UNKNOWN` / `GRANTED` / `DENIED` / `WITHDRAWN` |
| `consent_source`, `consent_at` | Where and when consent was captured |
| `do_not_call` | Hard suppression flag |
| `opted_out_at` | When the contact opted out |

These are writable through this API and the admin, and are **never** writable by CSV
import — re-uploading a stale list must not resurrect someone who opted out.

> The dialler is responsible for honouring `do_not_call` and `consent_status`. This
> module records them; it does not block calls.

---

## 1. List Contacts
**Endpoint:** `GET /api/v1/core/contacts/`

### Query parameters
* `page`, `page_size` — pagination.
* `search` — matches first name, last name, phone (raw and E.164), email, company.
* `ordering` — `created_at`, `updated_at`, `first_name`, `last_name`, `company` (prefix `-` to reverse).
* `is_active`, `do_not_call`, `consent_status`, `source`, `company` — exact-match filters.
* `tags` — comma-separated; matches contacts carrying **any** of these tags.
* `tags_all` — comma-separated; matches contacts carrying **all** of them.

### Response (200)
```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "0f0c2a2e-2a1e-4f5a-9a41-9d1f1a5a6b21",
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
      "created_at": "2026-07-29T09:12:00Z"
    }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

---

## 2. Create Contact
**Endpoint:** `POST /api/v1/core/contacts/`

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

`tags` are trimmed, de-duplicated, capped at 50 characters each, and limited to
`CONTACT_MAX_TAGS_PER_CONTACT` (default 25).

### Errors
| Status | Cause |
|---|---|
| 400 | Unparseable or invalid phone number |
| 400 | `phone_number` already belongs to another live contact in this tenant |
| 400 | Too many tags, or a tag over 50 characters |

Duplicates return a clean field error, not a 500:
```json
{
  "status": false,
  "message": "A contact with this phone number already exists.",
  "errors": { "phone_number": ["A contact with this phone number already exists."] }
}
```

---

## 3. Retrieve / Update / Delete

* `GET /api/v1/core/contacts/{id}/`
* `PATCH /api/v1/core/contacts/{id}/` — partial update
* `PUT /api/v1/core/contacts/{id}/` — full update
* `DELETE /api/v1/core/contacts/{id}/` — **soft** delete; frees the phone number

`POST /api/v1/core/contacts/bulk_delete/` accepts `{"ids": ["…", "…"]}`.

---

## 4. Look up by phone
**Endpoint:** `GET /api/v1/core/contacts/lookup/?phone=+14155552671`

Normalizes the supplied number and returns the matching contact. Intended for
inbound-caller resolution by the voice runtime.

* `phone` *(required)* — any format.
* `region` *(optional)* — ISO-3166 alpha-2, for non-international numbers.

| Status | Meaning |
|---|---|
| 200 | Contact found |
| 400 | Missing or unparseable number |
| 404 | No contact with that number in this tenant |

---

## 5. Tags in use
**Endpoint:** `GET /api/v1/core/contacts/tags/`

```json
{
  "status": true,
  "data": [ { "tag": "vip", "count": 42 }, { "tag": "lead", "count": 17 } ]
}
```
Sorted by count descending, then alphabetically.

---

## 6. Export
**Endpoint:** `GET /api/v1/core/contacts/export/?file_format=csv`

`file_format` is `csv` (default) or `xlsx`.

> Use **`file_format`**, not `format`. DRF reserves `format` for content negotiation and
> will reject `?format=csv` with a 404 before the endpoint runs.

Exported cells beginning with `=`, `+`, `-`, or `@` are prefixed with `'` so a contact
named `=HYPERLINK(...)` cannot execute when the file is opened in Excel.

---

## 7. Import (disabled here)
`POST /api/v1/core/contacts/import/` returns **405**. Bulk creation must go through the
staged flow so numbers are normalized and duplicates are reviewed:
see [Contact Import](contact_import.md).

---

## Linked records
`AgentCallLog.contact` and `BookingParticipant.contact` are resolved automatically from
their phone numbers when those records are written. Both use `SET_NULL`, so deleting a
contact never destroys call or booking history.

To link records created before this module existed:
```bash
python manage.py backfill_contact_links [--tenant-id <uuid>] [--batch-size 500]
```
Safe to re-run.
