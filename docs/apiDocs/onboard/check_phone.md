# Check Phone Availability API Documentation

Tells you whether a phone number is already taken, so a signup form can warn the user before they
submit. Part of the [signup flow](./signup_flow.md); safe to call on blur or with a debounce.

- **Path**: `POST /api/v1/onboard/check-phone/`
- **Auth**: none. Any `Authorization` header is ignored.
- **Side effects**: none. Nothing is written or reserved.

## Request

```json
{ "phone_number": "919876543211" }
```

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `phone_number` * | string | The number to test. | Required, non-empty. Leading and trailing whitespace is trimmed. **No format validation and no normalisation** — the value is matched as a literal string. |

> **Send the number in exactly the format you will submit to
> [create-order](./signup_flow.md#10-create-the-order).** Matching is literal, so
> `+91 98765 43211` and `919876543211` are different numbers to this endpoint. Checking one format
> and submitting another makes this call worthless.

> **The format rules are looser here than at submission.** This endpoint accepts any non-empty
> string; `create-order` requires 7–20 characters of digits, spaces, `+`, `(`, `)`, and `-`. A
> number can pass here and be rejected there.

## Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Checked. Read `data.available`. | `Phone number availability checked.` | `null` |
| **400** | `phone_number` missing or empty. | `Validation failed` | `{"phone_number": [...]}` |

**200 OK**
```json
{
  "status": true,
  "message": "Phone number availability checked.",
  "data": {
    "phone_number": "919876543211",
    "available": false,
    "exists_in_active_users": true,
    "exists_in_pending_registrations": false
  },
  "errors": null
}
```

### Response fields

| Field | Type | Meaning |
|---|---|---|
| `phone_number` | string | The trimmed value that was checked, echoed back. |
| `available` | boolean | `true` only when the number is free in **both** tables. This is the field to drive your UI from. |
| `exists_in_active_users` | boolean | Taken by a real account — **including soft-deleted ones**, which still hold their number. |
| `exists_in_pending_registrations` | boolean | Taken by an unfinished signup, which may have been abandoned. |

> **The two booleans exist so you can word the message usefully.** `exists_in_pending_registrations`
> usually means "you already started signing up" — offer
> [resume-session](./signup_flow.md#7-resume-a-session) rather than "number in use".

> **A soft-deleted account still reserves its number**, so `exists_in_active_users: true` does not
> imply anyone is actively using it. There is no way to release it through the API.

> **Advisory, not a reservation.** Someone can take the number between this call and
> `create-order`, which does its own check and returns
> `400 "A registration is already pending for this phone number."` Handle that regardless of what
> this returned.

> **Unlike [check-email](./signup_flow.md#1-check-email-availability), there is no Bloom-filter
> fast path** — this always queries the database. Both columns are indexed, so it is cheap, but
> debounce it rather than firing on every keystroke.

## Related

- [Signup flow](./signup_flow.md) — where the number is finally submitted
- [check-email](./signup_flow.md#1-check-email-availability) — the equivalent for email addresses
