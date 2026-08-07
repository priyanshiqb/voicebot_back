# Company Directory API Documentation

The tenant's **own people** — who works here, what they do, how to reach them. Backs the company
directory screen, with its Executives / Operations tabs and a search box.

- **Base path**: `/api/v1/core/directory-contacts/`
- **Auth**: JWT. Machine (`VB_AGENT`) tokens are refused.
- **Scope**: every response is filtered to the caller's tenant. Another tenant's entry is **404**,
  never 403.

> ## This is not `/core/contacts/`
>
> [`Contact`](./contact.md) is people the tenant **calls** — leads and customers, carrying consent
> status, `do_not_call`, and campaign membership. A `DirectoryContact` is a colleague, and is never a
> campaign target. The two tables share no rows and no code.
>
> If you are looking for the list a campaign draws from, you want the other one.

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/core/directory-contacts/` | List, filter, search |
| `POST` | `/core/directory-contacts/` | Add an entry |
| `GET` | `/core/directory-contacts/{id}/` | Retrieve one |
| `PATCH` | `/core/directory-contacts/{id}/` | Partial update |
| `PUT` | `/core/directory-contacts/{id}/` | Full replace |
| `DELETE` | `/core/directory-contacts/{id}/` | Soft delete |
| `POST` | `/core/directory-contacts/bulk_delete/` | Soft delete several by id |
| `GET` | `/core/directory-contacts/dropdown/` | `label`/`value` pairs |

---

## The category is stored, not inferred

`category` is a plain column the tenant sets: `EXECUTIVE` or `OPERATIONS`. **Nothing reads the job
title to decide it.**

That is worth stating because the obvious alternative — matching `designation` against a keyword list
like *head, vp, ceo, director* — fails in three ways this design avoids:

| Keyword matching | Stored column |
|---|---|
| Renaming *"Director of Ops"* to *"Ops Lead"* silently moves that person out of Executives | The category changes only when someone changes it |
| `head` matches *"Overhead Line Engineer"* | No false positives to have |
| Seven `ILIKE '%…%'` predicates cannot use an index — a sequential scan on every list request | `(tenant, category)` index lookup |

`OPERATIONS` is the default when the field is omitted, so a minimal create still lands somewhere
sensible.

---

## 1. List directory contacts

`GET /api/v1/core/directory-contacts/`

Ordered **alphabetically by name** — a directory is browsed, not monitored, so "most recently added"
is the wrong thing to put at the top. Paginated (`page`, `page_size`, max 100).

| Parameter | Type | Description |
|---|---|---|
| `category` | string | `EXECUTIVE` or `OPERATIONS`. **Omit for "All"** — there is no `category=ALL`. |
| `department` | string | Exact match |
| `is_active` | boolean | — |
| `search` | string | Matches `name`, `designation`, `email`, `phone_number` **and** `phone_e164` |
| `ordering` | string | `name`, `designation`, `category`, `created_at`, `updated_at` (prefix `-` to reverse) |
| `page`, `page_size` | int | — |

> **Search covers both phone columns on purpose.** Someone typing `98765` is reading the number off
> the screen — that is `phone_number`, as entered. Someone pasting `+919876543210` copied it from
> somewhere normalised. Searching only one of the two silently fails half the time.

**200 OK**
```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "b1c2d3e4-…",
      "name": "Jane Doe",
      "designation": "VP of Engineering",
      "category": "EXECUTIVE",
      "category_label": "Executive",
      "department": "Engineering",
      "email": "jane@acme.com",
      "phone_number": "+91 98765 43210",
      "phone_e164": "+919876543210",
      "is_active": true,
      "created_at": "2026-08-07T10:58:03Z"
    }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

> **Pagination lives in `meta`, not in `data`.** `data` is the array itself — this project's envelope
> throughout. A client expecting DRF's default `{count, next, previous, results}` inside `data` will
> read `undefined`.

The list uses a compact serializer: no `created_by` / `updated_by` / `deleted_by` blocks. Those are
three joins per row that the directory screen does not render. `GET /{id}/` returns them.

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more rows. | `success` |
| **401** | No valid JWT. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping. | `Your user account is not associated with any active tenant.` |

---

## 2. Add an entry

`POST /api/v1/core/directory-contacts/`

| Field | Type | Required | Description | Validation |
|---|---|---|---|---|
| `name` | string | ✔ | Full name, as displayed | Max 150. Trimmed; whitespace-only is rejected |
| `designation` | string | ✔ | Job title | Max 150. Trimmed |
| `category` | string | | `EXECUTIVE` or `OPERATIONS` | Defaults to `OPERATIONS` |
| `department` | string | | e.g. `Support` | Max 100 |
| `email` | string | | | Lower-cased. Unique within the tenant when present |
| `phone_number` | string | | As you want it displayed | Max 32. Never rejected — see below |

**Request**
```json
{
  "name": "Jane Doe",
  "designation": "VP of Engineering",
  "category": "EXECUTIVE",
  "department": "Engineering",
  "email": "jane@acme.com",
  "phone_number": "+91 98765 43210"
}
```

Returns the full object, including the derived `phone_e164` and the read-only `category_label`.

### One field is a single name, not first + last

Directory rows render as one line, and real titles — *"Dr. R. Sharma"*, *"Priya (Reception)"* — do
not split cleanly into first and last. `Contact` splits them because campaign personalisation needs
a first name; nothing here does.

### `phone_number` is never rejected

It is stored exactly as sent, and `phone_e164` is derived from it **best effort**. A number that
cannot be parsed leaves `phone_e164` as `""` — the entry still saves.

> This is deliberately unlike [`/core/contacts/`](./contact.md), where an unparseable number is a
> **400**. There the normalised number *is* the contact's identity, driving deduplication and
> inbound-caller lookup, so an invalid one has to be refused. Here it is a convenience for dialling,
> and `"x2214 (ask for Priya)"` is a real thing someone types about a real colleague. Losing that
> entry to gain a clean column would be the wrong trade.

Read `phone_e164` when you need to dial; read `phone_number` when you need to display.

### `email` is optional, and unique when present

Reception desks and shop-floor entries legitimately have no address, so requiring one would make
them un-addable. Uniqueness therefore applies **only to non-blank values** — any number of entries
may have no email.

It is lower-cased on the way in. The database compares the column directly, so without that
`Jane@acme.com` and `jane@acme.com` would be two rows to Postgres and one address to every human.

| Status | When | `message` / `errors` |
|---|---|---|
| **201** | Created. | `Created successfully` |
| **400** | `name` missing or blank. | `errors.name` |
| **400** | Another entry in this tenant already uses this email. | `errors.email: Another directory contact already uses this email address.` |
| **400** | `category` is not one of the two choices. | `errors.category` |
| **401 / 403** | As above. | — |

> **`tenant`, `phone_e164` and `category_label` are read-only.** Sending `tenant` is inert — the
> entry is always created in the caller's own tenant, so naming another tenant's id is not a way to
> write into it. Sending `phone_e164` is ignored; it is always derived.

---

## 3. Retrieve, update, delete

`GET`, `PATCH`, `PUT`, `DELETE` on `/api/v1/core/directory-contacts/{id}/`.

`PATCH` is the normal path. Fields not sent are left alone.

> **Editing `designation` does not change `category`.** They are independent columns. Promoting
> someone is two fields, and re-titling them is one — which is the whole point of storing the
> category rather than deriving it.

`DELETE` is a **soft delete**: the row is retained with `is_deleted = true` and disappears from every
list. The email address it held is released, so a colleague who leaves and returns can be re-added
with the same address.

| Status | When | `message` |
|---|---|---|
| **200** | Retrieved / updated / deleted. | `Retrieved successfully` / `Updated successfully` / `Deleted successfully` |
| **400** | Validation, as in §2. | — |
| **404** | Not this tenant's entry, or already deleted. | `Not found.` |

### Bulk delete

`POST /api/v1/core/directory-contacts/bulk_delete/` with `{"ids": ["…", "…"]}`.

| Status | When | `message` |
|---|---|---|
| **200** | All ids soft-deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, empty, or not a list. | `ids must be a non-empty list` |
| **400** | Any id is not this tenant's. **Nothing is deleted** — the check is all-or-nothing. | `Some ids are invalid` |

---

## 4. Dropdown

`GET /api/v1/core/directory-contacts/dropdown/`

`label` / `value` pairs for pickers, same shape as every other dropdown endpoint. `label` is
`"Name — Designation"`; `value` is the id.

---

## Building the directory screen

1. **Tabs** — `?category=EXECUTIVE`, `?category=OPERATIONS`, or no parameter for All.
2. **Search box** — `?search=`, debounced. It covers name, title, email and both phone columns.
3. **Paging** — read `meta.count` and `meta.next`, not `data`.
4. **Category control** — a two-option select, not free text. Its value is what the tabs filter on.

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **No import/export.** Unlike `Contact`, there is no CSV path. | A tenant with 200 staff adds them one at a time. |
| 2 | **No photo or avatar field.** | The UI has to fall back to initials. |
| 3 | **Any tenant user can edit the directory.** There is no role check. | Same gap as the [users API](../user/users.md) — visibility equals edit rights. |
| 4 | **`department` is free text.** | Two spellings of the same team do not group together. |

## Related

- [Contacts API](./contact.md) — people the tenant *calls*; the model this one is deliberately not
- [Users API](../user/users.md) — platform accounts, which directory entries are **not**: a person
  can appear in the directory without ever having a login
