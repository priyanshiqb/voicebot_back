# Tenant Email Configuration API Documentation

Lets a tenant send customer email from **their own** mail server instead of the platform's shared
account. A tenant may connect **several** configurations — a second provider to fail over to, a
replacement being tested before cutover — but **exactly one is the default**, and the default is the
one that will be used to reach their customers.

- **Base path**: `/api/v1/tenant/email-configurations/`
- **Auth**: JWT. Machine (`VB_AGENT`) tokens are refused — mail credentials are not in any agent's
  allowlist.
- **Scope**: every response is filtered to the caller's tenant. Another tenant's configuration is
  **404**, never 403.

> ## Booking email does not send until this is set up
>
> **There is no platform fallback.** A tenant with no configured, assigned, verified sender receives
> **no booking email at all** — no confirmations, no reminders, no cancellations, no reschedule
> requests. Bookings are still created; the messages are simply not sent, and each one leaves a
> `FAILED` row in `NotificationLog` explaining why.
>
> That is deliberate. A confirmation arriving from an address the customer has never heard of hurts
> the tenant's brand more than a missing one, and quietly lending the platform's sending reputation to
> a tenant's customer mail is what this whole feature exists to stop.
>
> Setup is two steps, not one: **create and verify a configuration here**, then **assign it** at
> [`/api/v1/tenant/settings/`](./settings.md). A verified configuration that has not been assigned
> sends nothing.

> **Platform authentication email is unaffected.** Signup OTP, email verification, and password reset
> always send on platform credentials — a tenant does not exist yet at signup, and a tenant whose SMTP
> breaks must not be locked out of the reset flow that would let them fix it.

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/tenant/email-configurations/` | List this tenant's configurations |
| `POST` | `/tenant/email-configurations/` | Add one. **The first becomes the default.** |
| `GET` | `/tenant/email-configurations/{id}/` | Retrieve one |
| `PATCH` | `/tenant/email-configurations/{id}/` | Partial update |
| `DELETE` | `/tenant/email-configurations/{id}/` | Remove. If it was the default, another is promoted. |
| `POST` | `/tenant/email-configurations/{id}/set-default/` | Promote this one |
| `POST` | `/tenant/email-configurations/{id}/disconnect/` | Take out of service, keeping the credentials |
| `POST` | `/tenant/email-configurations/{id}/reconnect/` | Put back into service |
| `POST` | `/tenant/email-configurations/{id}/test/` | Send a test message to **your own** address |
| `GET` | `/tenant/email-configurations/providers/` | Provider presets for the setup form |
| `GET` | `/tenant/email-configurations/dropdown/` | `label`/`value` pairs |
| `POST` | `/tenant/email-configurations/bulk_delete/` | Delete several by id |

> **There is no `PUT`.** A full replace of a credential record invites a client to omit `password`
> and wipe it. `PATCH` makes partial intent explicit.

---

## The default, and why it matters

| Rule | Behaviour |
|---|---|
| First configuration created | Automatically becomes the default |
| Second and later | Created without the flag — `is_default: false` |
| Promoting one | `set-default/` promotes it **and** demotes the previous default, atomically |
| Deleting the default | Another is promoted automatically, preferring a `VERIFIED`, connected one |
| **Disconnecting the default** | Same promotion — a connected replacement takes over |
| Deleting or disconnecting the last one | No usable default remains |

> **`is_default` is not what decides who sends your booking email.** That is the separate assignment
> at [`/tenant/settings/`](./settings.md). `is_default` marks the tenant's primary connection for
> display and for promotion when one is removed; the settings pointer is what the send path reads.
> They are usually the same configuration, and nothing requires them to be.

> **`is_default` and `is_active` are read-only in `POST` and `PATCH` bodies.** Sending them is
> silently ignored, not rejected. They move only through `set-default/`, `disconnect/`, and
> `reconnect/`. Writable flags would let a client demote or deactivate the default and skip the
> promotion — which fails *silently*: mail keeps flowing, from the platform address, until a customer
> asks why the confirmation came from someone else.

> **You cannot turn the default off.** You promote a different configuration, or disconnect this one.

> **"Has a default" is not the same as "has a working sender."** A disconnected configuration
> satisfies the one-default rule perfectly well and sends nothing. Anything checking a tenant's mail
> health must test `is_default AND is_active AND status == "VERIFIED"`.

---

## 1. List configurations

`GET /api/v1/tenant/email-configurations/`

Ordered **default first**, then newest. Paginated (`page`, `page_size`, max 100).

| Parameter | Type | Description |
|---|---|---|
| `provider` | string | Filter by provider, e.g. `?provider=SENDGRID` |
| `status` | string | `UNVERIFIED`, `VERIFIED`, `FAILED` |
| `is_default` | boolean | `?is_default=true` returns the single default |
| `is_active` | boolean | — |
| `search` | string | Matches `name`, `host`, `from_email` |
| `ordering` | string | `created_at`, `updated_at`, `name`, `status` (prefix `-` for descending) |

**200 OK**
```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "b1c2d3e4-…",
      "name": "Primary",
      "provider": "SENDGRID",
      "provider_label": "SendGrid",
      "is_default": true,
      "host": "smtp.sendgrid.net",
      "port": 587,
      "from_email": "bookings@acme.com",
      "from_name": "Acme Bookings",
      "status": "VERIFIED",
      "has_password": true,
      "is_active": true,
      "verified_at": "2026-08-06T11:04:22Z",
      "last_tested_at": "2026-08-06T11:04:22Z",
      "created_at": "2026-08-06T10:58:03Z"
    }
  ],
  "errors": null,
  "meta": { "count": 1, "next": null, "previous": null, "page": 1, "page_size": 10 }
}
```

| Status | When | `message` |
|---|---|---|
| **200** | Zero or more rows. An empty `data` array is normal — it means "using the platform sender". | `success` |
| **401** | No valid JWT. | `Authentication credentials were not provided.` |
| **403** | The user has no active tenant mapping. | `Your user account is not associated with any active tenant.` |

---

## 2. Create a configuration

`POST /api/v1/tenant/email-configurations/`

Stores credentials and, if this is the tenant's first, makes it the default. The configuration is
saved as `UNVERIFIED` — [test it](#6-send-a-test-message) before relying on it.

### Request fields

| Field | Type | Required | Description | Validation |
|---|---|---|---|---|
| `provider` | string | | Which mail service these credentials belong to. Fills any connection field you omit from a preset, and selects the right hints and error messages. | One of `SENDGRID`, `SES`, `POSTMARK`, `MAILGUN`, `BREVO`, `RESEND`, `GMAIL`, `ZOHO`, `HOSTINGER`, `OUTLOOK`, `CUSTOM`. Defaults to `CUSTOM`. |
| `name` | string | | Label shown in the list, e.g. `SendGrid — bookings`. Worth setting once a tenant has more than one. | Max 100 characters. Must be unique within the tenant (case-insensitive) if given. Blank is allowed, and more than one blank is allowed. |
| `host` | string | **\*** *(conditional)* | SMTP server hostname. **Optional when the chosen provider supplies one** — required for `SES` (region-dependent) and `CUSTOM`. | Max 255. Must resolve to a **public** address. Private, loopback, link-local, reserved and multicast addresses are rejected. An unresolvable hostname is rejected. |
| `port` | integer | | SMTP port. Taken from the preset, else `587`. | Must be `465`, `587`, or `2525`. **Port 25 is rejected** — cloud providers block it outbound, so it can only ever time out. |
| `username` | string | | SMTP login. **Varies by provider**: SendGrid expects the literal string `apikey`, Postmark expects its server token. Leave blank for relays that authenticate by IP. | Max 255. |
| `password` | string | | SMTP password or API key. **Write-only** — never returned by any endpoint. | Omit on `PATCH` to keep the stored value. Sending `""` also keeps it. |
| `use_tls` | boolean | | STARTTLS: connect in plaintext and upgrade. Pairs with port 587. Defaults to the preset, else `true`. | Cannot be `true` at the same time as `use_ssl`. |
| `use_ssl` | boolean | | Implicit TLS: encrypted from the first byte. Pairs with port 465. | Cannot be `true` at the same time as `use_tls`. At least one of the two must be on. |
| `from_email` | string | **\*** | The `From:` address recipients see. | Valid email address. **Most providers additionally require this address or its domain to be verified in their own dashboard** — validation here cannot check that; the [test send](#6-send-a-test-message) can. |
| `from_name` | string | | Sender display name. | Max 255. Falls back to the tenant's name when blank. |
| `reply_to` | string | | Where replies go, when that is not `from_email`. | Valid email address. |
> **`is_default`, `is_active`, and every lifecycle field (`status`, `verified_at`, `last_tested_at`,
> `last_used_at`, `last_error`) are read-only.** Sending them is ignored. Use `set-default/`,
> `disconnect/`, and `reconnect/`. New configurations are created connected.

**Request — the short form, letting the preset do the work**
```json
{
  "name": "Primary",
  "provider": "SENDGRID",
  "username": "apikey",
  "password": "SG.xxxxxxxxxxxxxxxxxxxx",
  "from_email": "bookings@acme.com",
  "from_name": "Acme Bookings"
}
```

**201 Created**
```json
{
  "status": true,
  "message": "Created successfully",
  "data": {
    "id": "b1c2d3e4-…",
    "tenant": { "id": "a0b1c2d3-…", "name": "Acme Corp", "subdomain": "acme" },
    "name": "Primary",
    "provider": "SENDGRID",
    "provider_label": "SendGrid",
    "is_default": true,
    "host": "smtp.sendgrid.net",
    "port": 587,
    "username": "apikey",
    "has_password": true,
    "use_tls": true,
    "use_ssl": false,
    "from_email": "bookings@acme.com",
    "from_name": "Acme Bookings",
    "reply_to": null,
    "status": "UNVERIFIED",
    "verified_at": null,
    "last_tested_at": null,
    "last_used_at": null,
    "last_error": "",
    "is_active": true,
    "created_by": { "id": "…", "email": "alex@acme.com" },
    "created_at": "2026-08-06T10:58:03Z"
  },
  "errors": null
}
```

Note that `host`, `port`, `use_tls`, and `use_ssl` came back **populated from the preset** even though
the request did not include them.

| Field | Meaning |
|---|---|
| `has_password` | Whether a password is stored. The password itself is never returned — not even masked, since a fixed-length mask still leaks length. |
| `provider_label` | Human-readable provider name, so a list does not have to map the enum. |
| `status` | `UNVERIFIED` until a test send succeeds. |

### Responses

| Status | When | `message` / `errors` |
|---|---|---|
| **201** | Created. `is_default` is `true` if this was the tenant's first. | `Created successfully` |
| **400** | `host` missing for `SES` or `CUSTOM`. | `A host is required for Amazon SES.` |
| **400** | `host` resolves to a private, loopback, or reserved address. | `'…' resolves to a private or reserved address (127.0.0.1) and cannot be used as a mail server.` |
| **400** | `host` cannot be resolved. | `'…' could not be resolved. Check the hostname.` |
| **400** | `port` not in `465`, `587`, `2525`. | `Port 25 is not allowed. Use one of: 465, 587, 2525.` |
| **400** | Both `use_tls` and `use_ssl` set. | `Use STARTTLS (use_tls) on port 587, or implicit TLS (use_ssl) on port 465 — not both.` |
| **400** | Neither `use_tls` nor `use_ssl` set. | `Encryption is required. Plaintext SMTP sends your password across the internet in the clear.` |
| **400** | `name` already used by another of this tenant's configurations. | `A configuration with this name already exists.` |
| **400** | `from_email` not a valid address, or `provider` unrecognised. | Field-specific |
| **401** | No valid JWT. | `Authentication credentials were not provided.` |
| **403** | No active tenant mapping. | `Your user account is not associated with any active tenant.` |

Every error is wrapped by the global exception handler — `{status, message, data, errors, request_id}`.

---

## 3. Retrieve a configuration

`GET /api/v1/tenant/email-configurations/{id}/`

| Parameter | Type | Description | Validation |
|---|---|---|---|
| `id` * | UUID | The configuration id. | Path parameter. |

| Status | When | `message` |
|---|---|---|
| **200** | Found. | `Retrieved successfully` |
| **404** | No such configuration **for this tenant**. Another tenant's id is indistinguishable from a nonexistent one. | `Not found.` |

---

## 4. Update a configuration

`PATCH /api/v1/tenant/email-configurations/{id}/`

Same fields as [create](#2-create-a-configuration). Everything is optional.

> **Omit `password` to keep the stored one.** Sending it only when you mean to rotate it is the
> intended pattern; a `PATCH` without the field never clears the credential.

> **There is no way to *remove* a password through the API** — an empty string is treated as "leave
> it alone". Delete the configuration if you need the credential gone.

> **Saving resets `status` to `UNVERIFIED`** when `host`, `port`, `username`, `password`, `use_tls`,
> `use_ssl`, or `from_email` changes — a changed server or credential invalidates the previous test.
> Editing only `name`, `from_name`, `reply_to`, or `is_active` leaves a `VERIFIED` configuration
> verified.

> **Changing `provider` re-applies that provider's preset** to any connection field you did not send
> in the same request. Switching `SENDGRID` → `HOSTINGER` moves the host to `smtp.hostinger.com`,
> the port to `465`, and flips the TLS flags to implicit TLS. Send explicit values if you want
> different ones.

| Status | When | `message` |
|---|---|---|
| **200** | Updated. | `Updated successfully` |
| **400** | Any validation failure from the [create table](#responses). | Field-specific |
| **404** | Not this tenant's configuration. | `Not found.` |

---

## 5. Promote a configuration

`POST /api/v1/tenant/email-configurations/{id}/set-default/`

No request body. Promotes this configuration and demotes the previous default in one transaction.

| Status | When | `message` |
|---|---|---|
| **200** | Promoted. The response carries the updated configuration. | `Default email configuration updated` |
| **404** | Not this tenant's configuration. | `Not found.` |

> **Promoting an `UNVERIFIED` configuration is allowed** — a tenant may want to line one up before
> testing. It will not be selected for real mail until it is `VERIFIED`.

---

## 5b. Disconnect and reconnect

`POST /api/v1/tenant/email-configurations/{id}/disconnect/`
`POST /api/v1/tenant/email-configurations/{id}/reconnect/`

No request body. Disconnecting takes a configuration out of service **without discarding its
credentials** — for an expired card, a suspected deliverability problem, or a provider migration in
progress.

> **This is deliberately not deletion.** A tenant pausing a provider should not have to source an API
> key again to come back. Delete when the credentials should be gone; disconnect when they should be
> idle.

> **Disconnecting the default promotes a connected replacement**, preferring a `VERIFIED` one. A
> pause must never silently drop a tenant onto the platform sender while a working alternative sits
> unused.

> **If nothing connected is left to promote, the disconnected configuration stays the default** —
> `is_default: true, is_active: false`. It is still the tenant's stated choice, and reconnecting it
> restores service with no further action. Display this state as *"no active sender"*, because it is.

> **A disconnected configuration can still be [tested](#6-send-a-test-message).** Proving a fix works
> before putting it back into service is the point.

**Reconnecting** puts it back. If the tenant's current default is disconnected, the reconnected one
**becomes** the default — restoring your only working sender should not also require remembering to
promote it. An already-connected default is left alone.

### The assigned booking sender is locked

**A configuration assigned as the booking sender cannot be disconnected or deleted.**

| Attempt | Result |
|---|---|
| `disconnect/` on the assigned sender | **400**, `errors.code = "BOOKING_SENDER_IN_USE"` |
| `DELETE /{id}/` on the assigned sender | **400**, same code |
| `bulk_delete` including the assigned sender | **400** — all-or-nothing, nothing is deleted |

The message names the way out:

> `This configuration is currently sending your booking email. Assign a different one at
> /api/v1/tenant/settings/ first, or clear the setting to stop sending booking email.`

Booking mail has no fallback, so taking the assigned sender out of service **stops those messages
entirely** rather than rerouting them. The tenant has to say what should carry them instead, or state
explicitly that nothing should.

> **A leaked credential is still an immediate fix.** Clear or reassign the setting, then disconnect —
> two calls, no waiting, and nothing forces you to delete credentials you may want to keep for
> investigation.

**Configurations that are not assigned are unrestricted.** Disconnect and delete spares freely.

| Status | When | `message` |
|---|---|---|
| **200** | Disconnected. Idempotent — disconnecting twice is fine. | `Email configuration disconnected` |
| **200** | Reconnected. | `Email configuration reconnected` |
| **400** | This configuration is the assigned booking sender. | See above; `errors.code = "BOOKING_SENDER_IN_USE"` |
| **404** | Not this tenant's configuration. | `Not found.` |

---

## 6. Send a test message

`POST /api/v1/tenant/email-configurations/{id}/test/`

No request body. Connects to the configured server and sends a short message.

> **The recipient is always your own account email.** There is no recipient parameter, and sending
> one is ignored. An authenticated endpoint that mails an arbitrary address on demand is a relay with
> extra steps — and the person configuring SMTP is the person who needs to see the result.

> **Testing a non-default configuration does not disturb the default.** This is most of the reason
> multiple configurations exist: a replacement provider can be proven *before* cutover rather than
> after.

> **Runs synchronously**, bounded by a 10-second connection timeout. Expect the request to take a
> couple of seconds.

**200 OK** — the configuration becomes `VERIFIED` and `verified_at` is set.
```json
{
  "status": true,
  "message": "Test message sent to alex@acme.com.",
  "data": { "id": "b1c2d3e4-…", "status": "VERIFIED", "verified_at": "2026-08-06T11:04:22Z", "…": "…" },
  "errors": null
}
```

**400 Bad Request** — the configuration becomes `FAILED` and `last_error` records the cause.
```json
{
  "status": false,
  "message": "The test message could not be sent.",
  "data": { "id": "b1c2d3e4-…", "status": "FAILED", "last_error": "The mail server rejected…", "…": "…" },
  "errors": { "detail": "The mail server rejected these credentials. Check the username and password — several providers expect a fixed username (SendGrid uses the literal word 'apikey') rather than your account login." }
}
```

### What the failure messages mean

| `errors.detail` says | Cause |
|---|---|
| *…rejected these credentials…* | Wrong username or password. Check the provider's username convention — several want a fixed literal, not your login. |
| *Microsoft 365 no longer accepts a username and password for SMTP…* | Basic authentication was retired in September 2025. Use a transactional provider or a certificate-based connector. Returned for any Microsoft host, whether the provider is `OUTLOOK` or `CUSTOM`. |
| *…refused '…' as a sender…* | `from_email` is not verified at the provider. Verify the address or domain in their dashboard. |
| *Could not connect to host:port…* | Wrong host, wrong port, or the wrong encryption mode for that port. |
| *…resolves to a private or reserved address…* | The hostname changed to point somewhere non-public since it was saved. |

| Status | When | `message` |
|---|---|---|
| **200** | Sent. Status becomes `VERIFIED`. | `Test message sent to {your email}.` |
| **400** | Rejected. Status becomes `FAILED`; the real error is in `errors.detail`. | `The test message could not be sent.` |
| **400** | Your user account has no email address. | `Your account has no email address to send the test to.` |
| **404** | Not this tenant's configuration. | `Not found.` |
| **429** | More than **5 attempts per hour** for this user. | `Request was throttled.` |

> **A success does not guarantee customer mail will arrive.** An Amazon SES account still in sandbox
> mode may only send to addresses verified in that account — and the tenant admin's own address
> usually is one. The test passes; real customer mail then fails. Check whether your SES account has
> production access.

---

## 7. Provider presets

`GET /api/v1/tenant/email-configurations/providers/`

Static reference data for the setup form — identical for every tenant, no stored configuration
involved. Use it to build the provider dropdown and to label the username and password fields
correctly per provider.

**200 OK**
```json
{
  "status": true,
  "message": "Provider presets retrieved successfully",
  "data": [
    {
      "value": "SENDGRID",
      "label": "SendGrid",
      "kind": "RELAY",
      "host": "smtp.sendgrid.net",
      "port": 587,
      "use_tls": true,
      "use_ssl": false,
      "host_required": false,
      "username_hint": "Enter the literal word: apikey",
      "password_hint": "Your SendGrid API key",
      "warning": null
    },
    {
      "value": "HOSTINGER",
      "label": "Hostinger Email",
      "kind": "MAILBOX",
      "host": "smtp.hostinger.com",
      "port": 465,
      "use_tls": false,
      "use_ssl": true,
      "host_required": false,
      "username_hint": "Your full email address, e.g. bookings@yourdomain.com",
      "password_hint": "Your mailbox password",
      "warning": "This is a personal or business mailbox, not a bulk sending service…"
    }
  ],
  "errors": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `value` / `label` | string | Dropdown pair. `value` is what you send as `provider`. |
| `kind` | string \| null | `RELAY`, `MAILBOX`, or `null` for `CUSTOM`. Group the dropdown by this — it is most of the guidance a tenant needs. |
| `host` | string \| null | The preset host. `null` when the tenant must supply one. |
| `port`, `use_tls`, `use_ssl` | — | Prefill values. |
| `host_required` | boolean | `true` for `SES` and `CUSTOM`. Show the host field as mandatory. |
| `username_hint` / `password_hint` | string | Field labels **for this provider**. The highest-value part of this endpoint. |
| `warning` | string \| null | Show before the tenant fills the form, not after they fail. |

### Relay vs mailbox

| Kind | Providers | Meaning |
|---|---|---|
| `RELAY` | SendGrid, Amazon SES, Postmark, Mailgun, Brevo, Resend | Transactional services built for volume. |
| `MAILBOX` | Gmail, Zoho, Hostinger, Microsoft 365 | Mailboxes for humans. **Daily and hourly send caps well below any relay's**, varying by plan. |

> **A mailbox provider will fail partway through a batch.** A tenant on Hostinger or Gmail whose
> bookings generate a few hundred reminders in a day hits the cap mid-run — earlier messages
> delivered, later ones rejected, with only a generic SMTP error to explain it. The `warning` on
> every `MAILBOX` entry says so up front.

> **No specific numeric caps are published here.** They differ per plan and change without notice; a
> stale figure would be worse than telling the tenant to check their plan.

| Status | When | `message` |
|---|---|---|
| **200** | Always, for any authenticated tenant user. | `Provider presets retrieved successfully` |
| **401** | No valid JWT. | `Authentication credentials were not provided.` |

---

## 8. Delete a configuration

`DELETE /api/v1/tenant/email-configurations/{id}/`

Soft delete. If the removed configuration was the default, **another is promoted automatically** —
preferring a `VERIFIED`, active one, since promoting something that has never completed a test send
would hand the tenant a default that cannot send.

| Status | When | `message` |
|---|---|---|
| **200** | Deleted. Refetch the list — the default may have moved. | `Deleted successfully` |
| **404** | Not this tenant's configuration. | `Not found.` |

Deleting the **last** configuration leaves the tenant with no default, which reverts them to the
platform sender. That is a valid state, not an error.

### Bulk delete

`POST /api/v1/tenant/email-configurations/bulk_delete/` with `{"ids": ["…", "…"]}`. Promotion runs
afterwards exactly as it does for a single delete.

| Status | When | `message` |
|---|---|---|
| **200** | All ids deleted. | `Bulk deleted successfully` |
| **400** | `ids` missing, empty, or not a list. | `ids must be a non-empty list` |
| **400** | Any id is not this tenant's. **Nothing is deleted** — the check is all-or-nothing. | `Some ids are invalid` |

---

## Setting up, end to end

1. `GET /providers/` → render the dropdown, grouped by `kind`.
2. User picks **SendGrid**; the form labels the username field *"Enter the literal word: apikey"*.
3. `POST /email-configurations/` with `provider`, `username`, `password`, `from_email`. Host, port
   and TLS come from the preset. Response has `is_default: true` if it was their first.
4. `POST /{id}/test/` → a message lands in their inbox and the status flips to `VERIFIED`.
5. On failure, show `errors.detail` verbatim. It is written for the tenant, and it goes only to the
   tenant that owns the credentials.
6. **`PATCH /api/v1/tenant/settings/` with `{"booking_email_configuration": "<id>"}`.** Until this
   step, booking email is still not sending. Confirm with `booking_email.will_send` in the response.

> **Step 4 is not the finish line.** A tenant who stops at `VERIFIED` has a green badge and no booking
> email. Steps 4 and 6 are one flow — do not present them as separate screens.

To swap providers later: create the new one, test it, assign it at
[`/tenant/settings/`](./settings.md), *then* disconnect the old one. That order matters — the assigned
sender is locked until it is released, and disconnecting keeps its credentials in case you need to
switch back.

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **A verified configuration does nothing until it is assigned** at [`/tenant/settings/`](./settings.md). | Setup looks complete — green `VERIFIED` badge — while booking email is still not sending. Show the assignment state, not the configuration status. |
| 2 | **Provider-side sender verification cannot be checked at save time.** | `from_email` passes validation and the provider rejects it at send. Only the test send catches it. |
| 3 | **SES sandbox mode is invisible to the test send.** | The test succeeds against the admin's own verified address; customer mail then fails. |
| 4 | **No per-tenant send rate limit.** | Once tenants use their own credentials, our shared cap no longer bounds how much mail leaves through our infrastructure. |
| 5 | **A password cannot be cleared, only replaced.** | An empty string means "keep". Removing a credential means deleting the configuration. |
| 6 | **Name uniqueness is case-insensitive in the API, case-sensitive in the database.** | The API rejects `sendgrid` when `SendGrid` exists; a direct database write would not. |
| 7 | **Microsoft 365 cannot work at all** with a username and password. The preset exists to explain the failure, not to enable it. | An `OUTLOOK` configuration will always fail its test. |

## Related

- [Booking API](../core/booking.md) — the notification lifecycle this will eventually serve
- [Calendar Integration API](../core/calendar_integration.md) — the same encrypted-credential pattern
- [Tenant SMTP integration plan](../../tenant-smtp-integration-plan.md) — design rationale and the
  remaining phases
