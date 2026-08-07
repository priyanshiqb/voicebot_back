# Tenant SMTP Integration — Implementation Plan

> **Status: ✅ Ways 1–3 IMPLEMENTED** (2026-08-06). Model + migration `tenant/0007`, admin,
> `utils/mail/{providers,validators,connection}.py`, serializers, viewset, urls under
> `/api/v1/tenant/email-configurations/`, settings, and **51 tests** in
> `tenant/tests/test_email_configuration.py`. API reference:
> [`apiDocs/tenant/email_configuration.md`](apiDocs/tenant/email_configuration.md).
>
> **Way 4 is IMPLEMENTED for booking email**, with a design decision that supersedes §11.2 below:
> **there is no platform fallback.** A tenant with no verified, assigned sender receives **no booking
> email at all**. `TenantSettings.booking_email_configuration` names the sender;
> `TenantSettings.booking_sender()` re-checks it at send time; `send_scheduling_email_task` resolves
> it inside the worker and refuses rather than falling back. Reference:
> [`apiDocs/tenant/settings.md`](apiDocs/tenant/settings.md).
>
> Platform authentication mail (signup OTP, verification, password reset) still uses the platform
> `EMAIL_*` settings and always will. Campaign mail does not exist yet.
>
> **Audience:** the engineer/agent implementing this.
> **Scope rule:** this document delivers a **project-wide mail capability**, not a booking feature.

---

## 0. Why this exists

Today every outbound email in the system — booking confirmations, reminders, cancellations, signup
OTPs, password resets — leaves from **one global Gmail account**:

```python
# core/tasks.py:40
def _default_from_email():
    sender_name = getattr(settings, "EMAIL_SENDER_NAME", "VoiceBot")
    address = getattr(settings, "EMAIL_HOST_USER", "") or "no-reply@localhost"
    return f"{sender_name} <{address}>"
```

Process-wide settings, no tenant argument. The `Tenant` model has no email, SMTP, or sender fields
at all. Three consequences:

| # | Problem | Bites when |
|---|---|---|
| 1 | **Gmail's send cap is per-account and shared across all tenants.** 500/day free, 2,000/day Workspace. | ~55 bookings/day *combined* on free tier (3 participants × 1 confirmation + 2 reminders = 9 messages/booking). Then it fails for **every** tenant at once. |
| 2 | **`.ics` `ORGANIZER` already disagrees with the SMTP sender.** [`ics.py:64`](../core/services/ics.py#L64) writes the host's address; `From:` is our Gmail. | Now. Outlook and Apple Calendar read organizer/sender mismatch as a spoofing signal. |
| 3 | **Shared IP, domain, and DMARC reputation.** | One tenant's spam complaints degrade deliverability for all of them. |

Letting a tenant supply their own SMTP credentials fixes all three, and is the smallest change that
does — no DNS work on our side, no provider migration, no per-tenant verification flow.

---

## 1. Scope

### In scope (Ways 1–3)

- Tenant-scoped, encrypted SMTP credential records — **several per tenant, exactly one default** (§3.0).
- A **single mail entry point** every app in the project can call — `utils/mail/`.
- **Provider presets** (§3.1) so a tenant picks "SendGrid" and pastes one key, rather than sourcing a
  hostname, a port, a TLS mode, and a username convention from four places.
- SSRF and abuse protection on tenant-supplied hosts (§4 — read it before writing code).
- A verification lifecycle: a config is not used for real mail until a test send has succeeded.
- CRUD + test-send API, admin, and API docs at the established standard.

### Explicitly NOT in scope

- **Changing any existing caller.** `core.tasks.send_scheduling_email_task` and
  `utils.tasks.send_email_task` keep their current behaviour until Way 4 is separately approved.
- Per-tenant sender *verification* against a transactional provider (SES/SendGrid identity flow).
- Per-tenant email templates, branding, or logos.
- Inbound mail, bounce webhooks, or open/click tracking.
- Replacing Gmail SMTP at the platform level. Still worth doing — see §12 — but independent of this.

### Hard rule that survives into every later phase

> **Platform authentication email never uses tenant SMTP.** Signup OTP, email verification, password
> reset, and forgot-password always send on platform credentials. Two reasons: a tenant does not
> exist yet at signup, and a tenant whose SMTP config breaks must never be locked out of the reset
> flow that would let them fix it.

This is encoded as `tenant=None` at the call site (§5.2) and asserted by a test (§9, T13).

---

## 2. Architecture

One resolver, one sender, everything else is a caller.

```
                     ┌──────────────────────────────────┐
   any app  ────────▶│  utils/mail/service.py           │
   (core, onboard,   │    send(...) / get_connection()  │
    user, campaigns) └───────────┬──────────────────────┘
                                 │
                     ┌───────────▼──────────────────────┐
                     │  utils/mail/resolver.py          │
                     │    resolve(tenant) -> Sender     │
                     └───────────┬──────────────────────┘
                        tenant?  │  no / unverified / inactive
                       verified  │           │
                                 ▼           ▼
                    TenantEmailConfiguration    settings.EMAIL_* (platform)
                    (tenant/models, encrypted)
```

**Placement decisions and why:**

| Piece | Lives in | Reason |
|---|---|---|
| `TenantEmailConfiguration` model | **`tenant/`** app | It is tenant configuration, and `core`/`onboard`/`user` already depend on `tenant` — so the dependency arrow points the right way. `tenant` has migrations (latest `0006`); `utils` is not an installed app and cannot hold models. |
| Resolver + send service | **`utils/mail/`** | Must be importable from every app without pulling in `core`. `utils/tasks.py` already sits here and is imported by `onboard` and `user`. The model import inside the resolver is **lazy** (function-level), matching the repo's existing style — no circular import at module load. |
| ViewSet, serializers, urls | **`tenant/`** app | New `tenant/views/`, `tenant/serializers/`, `tenant/urls/` REST surface. The app currently has only websocket routing; this adds the first REST router to it. |

---

## 3. Data model

`tenant/models/TenantEmailConfiguration.py`, extending **`TenantBaseModel`** (Tier 2 — who changed
mail credentials is exactly the kind of thing that needs `created_by`/`updated_by`).

```python
class TenantEmailConfiguration(TenantBaseModel):
    class Status(models.TextChoices):
        UNVERIFIED = "UNVERIFIED", "Unverified"   # saved, never successfully test-sent
        VERIFIED   = "VERIFIED",   "Verified"     # a test send succeeded
        FAILED     = "FAILED",     "Failed"       # a real send failed permanently

    class Provider(models.TextChoices):
        SENDGRID = "SENDGRID", "SendGrid"
        SES      = "SES",      "Amazon SES"
        POSTMARK = "POSTMARK", "Postmark"
        MAILGUN  = "MAILGUN",  "Mailgun"
        BREVO    = "BREVO",    "Brevo"
        RESEND   = "RESEND",   "Resend"
        GMAIL     = "GMAIL",     "Gmail / Google Workspace"
        ZOHO      = "ZOHO",      "Zoho Mail"
        HOSTINGER = "HOSTINGER", "Hostinger Email"
        OUTLOOK   = "OUTLOOK",   "Microsoft 365 / Outlook"
        CUSTOM    = "CUSTOM",    "Custom SMTP"
```

| Field | Type | Null | Notes |
|---|---|---|---|
| `tenant` | FK | no | From `TenantBaseModel`. **Not unique** — a tenant may hold several configurations. |
| `name` | `CharField(100)` | yes (`blank`) | Label for the list, e.g. `SendGrid — bookings`. Unique per tenant where non-blank. |
| `provider` | `CharField(choices)` | no | Default `CUSTOM`. Drives the form preset (§3.1) and error messaging. **Never read by the send path.** |
| `is_default` | `BooleanField` | no | Default `False`. The configuration used to reach this tenant's customers. **Exactly one per tenant** — see §3.0. |
| `host` | `CharField(255)` | no | SMTP hostname. Validated per §4. |
| `port` | `PositiveIntegerField` | no | Default `587`. Must be in the allowlist (§4.2). |
| `username` | `CharField(255)` | yes (`blank`) | Some relays authenticate by IP; allow empty. |
| `password` | `TextField` | yes (`blank`) | **Encrypted at rest.** Never serialized out. |
| `use_tls` | `BooleanField` | no | Default `True` (STARTTLS, port 587). |
| `use_ssl` | `BooleanField` | no | Default `False` (implicit TLS, port 465). Mutually exclusive with `use_tls`. |
| `from_email` | `EmailField` | no | Envelope + header sender. |
| `from_name` | `CharField(255)` | yes (`blank`) | Display name. Falls back to `tenant.name`. |
| `reply_to` | `EmailField` | yes | Optional; useful when `from_email` is a no-reply box. |
| `status` | `CharField(choices)` | no | Default `UNVERIFIED`. |
| `is_active` | `BooleanField` | no | Inherited. Lets a tenant park a config without deleting it. |
| `verified_at` | `DateTimeField` | yes | Set on the first successful test send. |
| `last_tested_at` | `DateTimeField` | yes | Every test attempt, pass or fail. |
| `last_used_at` | `DateTimeField` | yes | Last real (non-test) send. Written at most once/minute — see below. |
| `last_error` | `TextField` | yes (`blank`) | Truncated to 2000 chars. Same convention as `CalendarIntegration.last_error`. |

```python
class Meta:
    db_table = "tenant_email_configurations"
    constraints = [
        models.UniqueConstraint(
            fields=["tenant"],
            condition=models.Q(is_default=True, is_deleted=False),
            name="unique_default_tenant_email_configuration",
        ),
        models.UniqueConstraint(
            fields=["tenant", "name"],
            condition=models.Q(is_deleted=False) & ~models.Q(name=""),
            name="unique_tenant_email_configuration_name",
        ),
    ]
```

### 3.0 Many configurations, exactly one default

A tenant may connect several providers — a second one to fail over to, a replacement being tested
before cutover, eventually a separate sender per purpose. **One of them is the default**, and the
default is what reaches that tenant's customers.

The two halves of "exactly one" live in different places, because only one of them can be a database
constraint:

| Half | Enforced by | Why there |
|---|---|---|
| **At most one default** | Partial unique index on `(tenant)` where `is_default AND NOT is_deleted` | Two concurrent promotions must lose at the database, not at whichever check ran first. |
| **At least one default, when any config exists** | `make_default()` / `ensure_default()` | No constraint can require a row to *exist*. |

Rules the application layer owns:

- **The first configuration a tenant creates becomes the default automatically.** Nobody sets up mail
  and then wonders why none is being sent.
- **Promotion clears the previous default and sets the new one in one transaction, in that order.**
  Reversed, both rows are briefly `is_default=True` and the index rejects the write. Split across two
  transactions, a crash between them leaves the tenant with *no* default.
- **Deleting or disconnecting the default promotes another** — preferring a `VERIFIED`, connected
  one, since promoting something that has never completed a test send hands the tenant a default that
  cannot send.
- **`is_default` and `is_active` cannot be set directly.** They move through `set-default/`,
  `disconnect/`, and `reconnect/`. An endpoint that lets a tenant switch either flag off in a `PATCH`
  skips the promotion and leaves them with no usable sender.
- **"Has a default" is not "has a working sender."** A disconnected default satisfies the unique
  constraint and sends nothing, so `ensure_default` tests **usable**, not merely present. This is the
  distinction the first implementation got wrong.
- **When nothing connected is left to promote, the disconnected default is kept** rather than
  demoted: it is still the tenant's stated choice, reconnecting restores service with no further
  action, and demoting would leave the row set with no default at all.
- **Zero configurations means no default**, and the tenant falls back to the platform sender. That is
  the correct resting state, not an error.

> **Losing the default is a silent failure, which is what makes it worth this much care.** Mail keeps
> flowing — on the platform sender, from the wrong address — until a customer asks why the booking
> confirmation came from someone else.

> **`last_used_at` must not be written on every send.** A tenant sending 500 reminders would issue
> 500 UPDATEs against one row and serialise the whole batch on a single lock. Write it only when the
> stored value is more than 60s old, with a targeted `.filter(pk=...).update(...)` — never
> load-modify-save from a task.

### 3.1 Provider presets

`utils/mail/providers.py` — a plain dict, no database table. Most support tickets on a feature like
this are one of two things: someone putting their SendGrid *login* in the username field, or someone
guessing a port. A preset removes both.

| `provider` | Kind | Host | Port | Mode | Username is… | Password is… |
|---|---|---|---|---|---|---|
| `SENDGRID` | relay | `smtp.sendgrid.net` | 587 | STARTTLS | the literal string **`apikey`** | the API key |
| `SES` | relay | `email-smtp.<region>.amazonaws.com` — **tenant must supply** | 587 | STARTTLS | SES SMTP username | SES SMTP password (**not** an AWS access key) |
| `POSTMARK` | relay | `smtp.postmarkapp.com` | 587 | STARTTLS | the Server API token | the same Server API token |
| `MAILGUN` | relay | `smtp.mailgun.org` | 587 | STARTTLS | `postmaster@<your-domain>` | the SMTP password |
| `BREVO` | relay | `smtp-relay.brevo.com` | 587 | STARTTLS | the account login email | the SMTP key |
| `RESEND` | relay | `smtp.resend.com` | 587 | STARTTLS | the literal string **`resend`** | the API key |
| `GMAIL` | mailbox | `smtp.gmail.com` | 587 | STARTTLS | the full mailbox address | an **App Password** — requires 2FA |
| `ZOHO` | mailbox | `smtp.zoho.com` | 465 | SSL | the full mailbox address | an app password |
| `HOSTINGER` | mailbox | `smtp.hostinger.com` | 465 | SSL | the full mailbox address | the mailbox password |
| `OUTLOOK` | mailbox | `smtp.office365.com` | 587 | STARTTLS | the mailbox address | *see the warning below* |
| `CUSTOM` | — | tenant supplies | tenant supplies | tenant supplies | — | — |

Each entry carries a `username_hint` and `password_hint` string. The API returns them (§6.4) so the
frontend labels the fields correctly per provider instead of showing a generic "Username".

> **Hostinger also accepts `587` with STARTTLS.** The preset uses `465`/SSL because that is what
> Hostinger's own setup pages lead with, and a tenant copying from those pages should find the form
> already matching. Either combination works — a tenant who switches to 587 must also flip `use_tls`
> on and `use_ssl` off, which is why the form should present this as one "encryption" choice rather
> than two independent booleans.

#### Relay vs mailbox — the distinction that matters operationally

`GMAIL`, `ZOHO`, `HOSTINGER`, and `OUTLOOK` are **mailboxes for humans**, not transactional relays.
They enforce daily and hourly send caps far below any relay's, and those caps vary by plan. A tenant
who picks Hostinger and then runs a campaign, or whose bookings generate a few hundred reminders in a
day, will hit a wall — and the failure arrives as a generic SMTP rejection partway through a batch,
with the earlier messages already delivered.

Every mailbox-kind preset therefore carries a non-null `warning` (§6.4):

> `This is a personal or business mailbox, not a bulk sending service. Your provider limits how many
> messages you can send per day, and that limit varies by plan. For campaigns or high booking volume,
> use a transactional provider such as SendGrid, Amazon SES, or Postmark.`

Do **not** try to encode specific numeric caps in the preset. They differ per plan, change without
notice, and a stale number in our UI is worse than an honest "check your plan".

> **The preset is a form default, not a runtime lookup.** On save, the chosen preset's host, port,
> and TLS flags are **materialised onto the row**, and the resolver reads only the columns. If it
> resolved the preset at send time, editing this dict in a future release would silently repoint
> every existing tenant's mail — a config that worked on Tuesday failing on Wednesday with no change
> the tenant can see.

> **Explicit values in the request always win over the preset.** A tenant on `SENDGRID` who supplies
> `port: 2525` gets 2525. The preset fills gaps; it does not overwrite intent.

> **`SES` and `CUSTOM` require an explicit `host`.** SES because the hostname is region-dependent and
> guessing a region silently sends to the wrong one. Reject the save with a field error naming the
> expected shape, rather than defaulting to `us-east-1`.

#### The Microsoft 365 problem

`OUTLOOK` is in the list to produce a **useful failure**, not because it works.

Microsoft permanently disabled Basic authentication for Client Submission (SMTP AUTH) in Exchange
Online from September 2025. A mailbox username and password cannot authenticate — OAuth2 is required,
which this feature does not and will not provide.

The save is still allowed: hybrid and on-premises Exchange deployments behind the same hostname
convention do still accept basic auth, and blocking them to stop a likely mistake is the wrong trade.
What must happen is the **error message**, keyed on the *host* rather than the preset so a `CUSTOM`
config pointed at `office365.com` gets the same help:

> `Microsoft 365 no longer accepts a username and password for SMTP (basic authentication was
> retired in September 2025). Use a transactional provider such as SendGrid, Amazon SES, or
> Postmark, or configure a certificate-based Exchange connector.`

Verify the current state of this against Microsoft's documentation before writing the final copy —
it moved recently and may move again.

### 3.2 Encryption

Reuse [`core/services/crypto.py`](../core/services/crypto.py) exactly as `CalendarIntegration` does —
Fernet with `MultiFernet` key rotation already built in.

```python
def set_password(self, raw):
    from core.services import crypto
    self.password = crypto.encrypt(raw) if raw else ""

def get_password(self):
    from core.services import crypto
    return crypto.decrypt(self.password)
```

Two caveats to handle, not ignore:

1. **The module lives in `core` but the model lives in `tenant`.** The import is lazy (inside the
   method), so it does not invert the app dependency at load time. If that still feels wrong, move
   `crypto.py` to `utils/crypto.py` and re-export from `core.services.crypto` for the existing
   caller — a two-line change, and arguably where it belonged anyway.
2. **The key setting is named `SCHEDULING_TOKEN_ENCRYPTION_KEY`.** It is no longer scheduling-only.
   Introduce `APP_ENCRYPTION_KEY` that falls back to the old name, so nothing breaks and new deploys
   get a sane name. Do not rename the env var in place — that silently bricks decryption of every
   stored calendar token.

---

## 4. Security — read before writing code

A tenant supplies a hostname and a port, and our worker connects to it. That is **SSRF by design**.
Without the controls below, any tenant user can point `host` at `169.254.169.254` (cloud metadata),
`127.0.0.1:6379` (your Redis), or an internal service, and use our infrastructure as a probe.

### 4.1 Host validation

```python
# utils/mail/validators.py
def validate_smtp_host(host):
    """Reject hosts that resolve to non-public addresses. Raises ValidationError."""
```

- Resolve with `socket.getaddrinfo(host, None)`.
- Reject if **any** resolved address is `is_private`, `is_loopback`, `is_link_local`,
  `is_reserved`, `is_multicast`, or `is_unspecified` (`ipaddress` module handles v4 and v6).
- Reject a literal IP in `host` under the same test.
- Reject on DNS failure — a host we cannot resolve is a host we should not dial.

> **Check at save time *and* at send time.** Save-time-only validation is defeated by DNS rebinding:
> the tenant points a domain at a public IP, passes validation, then repoints it at `127.0.0.1`.
> The send-time check is the one that actually protects you.

> **Cache the send-time verdict** in Redis for 5 minutes, keyed `smtp_host_ok:<host>`. Re-resolving
> on every message adds a DNS round trip to each send; 5 minutes bounds the rebinding window to
> something an attacker cannot usefully exploit while keeping the hot path fast.

### 4.2 Port allowlist

`{465, 587, 2525}` — configurable via `TENANT_SMTP_ALLOWED_PORTS`. Anything else is rejected at save.
This is what stops `port=6379` / `port=5432` probing.

> **Port 25 is deliberately excluded.** AWS, GCP, and Azure all block outbound port 25 by default, so
> allowing it buys a 10-second timeout and a confusing "connection failed" instead of an immediate,
> accurate field error. No provider needs 25 for authenticated submission — 587 and 465 are the
> submission ports, and 2525 is the common fallback where 587 is filtered.

### 4.3 Connection timeout — non-negotiable

Django's SMTP backend defaults to **`timeout=None`, which blocks forever.** A tenant pointing at a
host that accepts the TCP connection and then goes silent will hang a Celery worker permanently. Every
`get_connection()` call in this feature **must** pass `timeout=settings.TENANT_SMTP_TIMEOUT_SECONDS`
(default `10`).

### 4.4 Local development

The rules above block MailHog / Mailpit on `localhost`, which every developer runs. Provide
`TENANT_SMTP_ALLOW_PRIVATE_HOSTS` (default `False`). It must be **`False` in
`config/settings/production.py` with no env override path** — a flag that can be flipped by
environment in production is not a control.

### 4.5 Credential handling

- `password` is `write_only=True` on the serializer. It is **never** in a response, ever.
- Reads expose `has_password: true|false`, not a mask — a mask of fixed length still leaks length.
- `TenantAdmin` shows the field as `""` and only writes when non-empty (§7).
- **A `PATCH` that omits `password` must not wipe it.** This is the single easiest bug to ship here:
  a partial update that reaches `set_password(None)` silently blanks a working config. Explicitly
  `pop` the key when absent.
- Never log the password, and never echo raw SMTP protocol errors to a *different* tenant. Echoing
  them to the owning tenant is correct and necessary — see §6.

### 4.6 Abuse: the test-send endpoint

An authenticated endpoint that sends mail to an arbitrary address is an open relay with extra steps.

> **v1: the test send goes to `request.user.email` only.** No recipient in the request body. This is
> not a limitation worth negotiating — the tenant admin configuring SMTP is the person who needs to
> see the result, and it removes the abuse case entirely.

Throttle it regardless: `ScopedRateThrottle`, scope `smtp_test`, **5/hour per user**.

### 4.7 `use_tls` / `use_ssl`

Django raises `ValueError` if both are `True`. Validate in the serializer and return a clean 400
rather than letting a 500 escape from deep inside the mail backend.

Also reject `use_tls=False, use_ssl=False` unless `TENANT_SMTP_ALLOW_PRIVATE_HOSTS` is on —
plaintext SMTP over the public internet transmits the tenant's password in the clear.

---

## 5. The mail service — `utils/mail/`

```
utils/mail/
    __init__.py       # public API: send, get_sender, MailConfigurationError
    resolver.py       # tenant -> Sender
    service.py        # send() and connection construction
    validators.py     # §4.1 host / port / TLS validation
```

### 5.1 The resolver

```python
@dataclass(frozen=True)
class Sender:
    connection_kwargs: dict | None   # None -> use the platform default backend
    from_email: str                  # "Name <addr>", fully formatted
    reply_to: list[str]
    config_id: str | None            # for marking FAILED later; None for platform
```

```python
def resolve(tenant=None):
    """Return the Sender for this tenant, falling back to the platform sender."""
```

Fallback ladder — a config is used **only** when every condition holds:

| Condition | Else |
|---|---|
| `tenant` is not `None` | platform |
| `settings.TENANT_SMTP_ENABLED` | platform |
| The tenant has a **default** config (`is_default=True`, not deleted) | platform |
| `config.is_active` | platform |
| `config.status == VERIFIED` | platform |
| Send-time host validation passes (§4.1) | platform + mark `FAILED` |

> **The resolver reads only the default.** It never picks "the most recently updated" or "the first
> verified" among a tenant's configurations — a rule like that sends customer mail from a different
> address depending on what someone edited that afternoon. Non-default configurations are inert until
> promoted.

> **`UNVERIFIED` never sends.** A tenant who saves credentials and walks away keeps working mail on
> the platform sender rather than silently dropping every message into a misconfigured relay.

Returning `connection_kwargs` (a plain dict) rather than a live connection object is deliberate:
**an SMTP connection is not JSON-serialisable and cannot cross a Celery boundary.** Tasks receive a
`tenant_id` and resolve inside the worker. Any design that tries to pass a connection through
`.delay()` will fail at enqueue time.

### 5.2 The send API

```python
def send(*, subject, body, recipient_list, tenant=None, html_body=None,
         attachments=None, reply_to=None, headers=None):
    """Send one message. `tenant=None` means platform credentials — which is
    correct and required for signup, verification, and password-reset mail."""
```

Returns a small result object (`sent: bool`, `used_tenant_config: bool`, `error: str`) rather than
raising for delivery failure — callers decide their own retry policy, and both existing tasks already
have one.

### 5.3 Failure classification

| SMTP outcome | Action |
|---|---|
| `SMTPAuthenticationError` (535) on a Microsoft host | Permanent. `status=FAILED`, and return the **named M365 message** from §3.1 rather than the raw error. Keyed on host (`*.office365.com`, `*.outlook.com`), not on `provider`. |
| `SMTPAuthenticationError` (535), `SMTPNotSupportedError` | Permanent. Set `status=FAILED`, write `last_error`, notify tenant admins (reuse `core.services.push.notify_tenant_admins`). |
| `SMTPSenderRefused` (550/553) | Permanent. Almost always an unverified sender at the provider (§6.3) — say so, rather than passing through "Sender address rejected". |
| `SMTPRecipientsRefused` | Permanent for **that recipient**, not the config. Do not mark the config failed. |
| `SMTPServerDisconnected`, `SMTPConnectError`, `socket.timeout` | Transient. Let the caller's retry policy run. Mark `FAILED` only after the caller exhausts retries. |
| Host validation failure at send time | Permanent. `status=FAILED`, and **do not attempt the connection**. |

> **What happens to the message when a config goes `FAILED` mid-send is a Way 4 decision**, not a
> Way 1–3 one. Way 1–3 only classifies and records. Options are laid out in §11.

---

## 6. API

Base path `/api/v1/tenant/email-configurations/`. A **collection** — a tenant holds several, exactly
one of which is the default (§3.0).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/email-configurations/` | List this tenant's configurations |
| `POST` | `/email-configurations/` | Add one. **The first one created becomes the default.** |
| `GET` | `/email-configurations/{id}/` | Retrieve one |
| `PATCH` | `/email-configurations/{id}/` | Partial update |
| `DELETE` | `/email-configurations/{id}/` | Remove. If it was the default, another is promoted (§3.0). |
| `POST` | `/email-configurations/{id}/set-default/` | Promote this one; demotes the previous default |
| `POST` | `/email-configurations/{id}/disconnect/` | Out of service, credentials retained; promotes a replacement |
| `POST` | `/email-configurations/{id}/reconnect/` | Back into service; becomes default if none is connected |
| `POST` | `/email-configurations/{id}/test/` | Send a test message to the caller's own address |
| `GET` | `/email-configurations/providers/` | The preset catalogue, for the setup form (§6.4) |

A standard `TenantBaseViewSet`, so `get_queryset()` tenant scoping applies unchanged. **The tenant
comes from `request.user.tenant_mapping`, never from the request body** — a `tenant` field in the
payload must be ignored, not honoured.

> **`is_default` is read-only on create and update.** It moves only through `set-default/`, and
> through the automatic promotion rules in §3.0. A writable boolean lets a tenant clear it and end up
> with no default, or set it on a second row and hit the unique index as an unhandled `IntegrityError`.

> **`test/` is per-configuration**, so a tenant can verify a replacement provider while their current
> default keeps serving live mail. This is most of why multiple configurations are worth supporting:
> cutover stops being a leap.

### 6.1 Request fields (`POST` / `PATCH`)

| Field | Type | Required | Description | Validation |
|---|---|---|---|---|
| `name` | string | | Label for this connection in the list, e.g. `SendGrid — bookings`. | Max 100. Must be unique within the tenant if given. |
| `provider` | string | | Which mail service these credentials belong to. Fills any host/port/TLS field you omit (§3.1) and selects the right field hints and error messages. Defaults to `CUSTOM`. | One of `SENDGRID`, `SES`, `POSTMARK`, `MAILGUN`, `BREVO`, `RESEND`, `GMAIL`, `ZOHO`, `HOSTINGER`, `OUTLOOK`, `CUSTOM`. |
| `host` | string | **\*** *(conditional)* | SMTP server hostname, e.g. `smtp.sendgrid.net`. **Optional when `provider` supplies one** — required for `CUSTOM` and `SES`. | Max 255. Must resolve to a public address (§4.1). |
| `port` | integer | | SMTP port. From the preset, else `587`. | Must be one of `465`, `587`, `2525`. Port `25` is rejected (§4.2). |
| `username` | string | | SMTP login. Leave empty for IP-authenticated relays. | Max 255. |
| `password` | string | | SMTP password or API key. **Write-only** — never returned. | Omit on `PATCH` to keep the stored value. |
| `use_tls` | boolean | | STARTTLS. Default `true`. Pair with port 587. | Cannot both be `true` with `use_ssl`. |
| `use_ssl` | boolean | | Implicit TLS. Default `false`. Pair with port 465. | Cannot both be `true` with `use_tls`. |
| `from_email` | string | **\*** | The `From:` address recipients see. | Valid email. |
| `from_name` | string | | Display name. Defaults to the tenant's name. | Max 255. |
| `reply_to` | string | | Where replies go, if not `from_email`. | Valid email. |
| `is_active` | boolean | | Whether to use this config. Default `true`. | — |

> **Saving always resets `status` to `UNVERIFIED`.** Changing the host or password invalidates the
> previous verification, and treating an edit as still-verified is how a broken config keeps being
> selected for real mail.

### 6.2 Responses

| Status | When | `message` |
|---|---|---|
| **200** | `GET` list or detail, or a successful `PATCH`. | `success` |
| **200** | `GET` list with no configurations. `data` is `[]` — **not** a 404; "none configured" is a valid state meaning "platform sender". | `success` |
| **200** | `set-default/` succeeded. Response carries the promoted config. | `Default email configuration updated` |
| **201** | Config created. Response includes `is_default: true` when it was the tenant's first. | `Email configuration saved` |
| **204** | Deleted. If it was the default, another has already been promoted — the client should refetch the list. | — |
| **400** | `name` collides with another of this tenant's configurations. | `A configuration with this name already exists.` |
| **400** | Validation failure: bad port, unresolvable/private host, both TLS flags, invalid email. | Field-specific |
| **400** | `provider` is `CUSTOM` or `SES` and no `host` was given. | `host is required for this provider.` |
| **400** | `provider` is not a recognised value. | Field-specific |
| **401** | No valid credentials. | `Authentication credentials were not provided.` |
| **403** | The user has no active tenant mapping. | `Your user account is not associated with any active tenant.` |
| **429** | Test-send throttle exceeded (5/hour). | `Request was throttled.` |

Every error is wrapped by the global `EXCEPTION_HANDLER` envelope — `{status, message, data, errors,
request_id}` — as everywhere else in this API.

### 6.3 Test send

`POST /email-configuration/test/` — empty body.

| Status | When | Meaning |
|---|---|---|
| **200** | SMTP accepted the message. | `status` becomes `VERIFIED`, `verified_at` set. The config is now eligible for real mail. |
| **400** | SMTP rejected it. | `status` stays `UNVERIFIED`/`FAILED`; `errors.detail` carries the **real SMTP error**, truncated. |
| **404** | No configuration with that id for this tenant. | `Not found.` |
| **429** | More than 5 attempts this hour. | — |

> **Return the real SMTP error to the owning tenant.** "Configuration test failed" is useless — the
> difference between `535 Authentication failed` and `Connection refused` is the entire diagnostic
> value. It goes only to the tenant that owns the config, which is not a disclosure.

> **Run the test synchronously**, not through Celery. The caller is a human waiting on a form, and
> the §4.3 timeout already bounds it at 10 seconds.

#### What the test catches that validation cannot

Field validation proves the credentials are *well-formed*. Only a real send proves the provider will
accept them. Three failures are common enough to name explicitly in the response:

| Failure | Why field validation misses it |
|---|---|
| **Unverified sender.** SendGrid, SES, Postmark, and Mailgun all reject mail from a `from_email` whose address or domain has not been verified in *their* dashboard. | `from_email` is a syntactically valid address. We have no way to know what the provider has verified. |
| **M365 basic auth.** §3.1. | The credentials are well-formed; the server refuses the authentication method. |
| **Wrong username convention.** SendGrid wants the literal `apikey`; Postmark wants the token twice. | Any non-empty string passes validation. |

> **Amazon SES sandbox is the one the test send *cannot* catch.** A new SES account may only send to
> addresses verified in that account. The tenant admin's own address is usually one of them — so the
> test passes, and real customer mail then fails. Call this out in the API doc and in the SES preset's
> hint text: *"If your SES account is still in sandbox mode, this test will succeed but mail to your
> customers will not."*

### 6.4 Provider presets endpoint

`GET /api/v1/tenant/email-configuration/providers/` — the §3.1 catalogue, so the setup form is not a
hardcoded copy of a dict that lives in the backend.

Static reference data, identical for every tenant, no stored config involved. JWT required (it is a
route on a tenant viewset), but nothing tenant-specific is returned.

```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "value": "SENDGRID",
      "label": "SendGrid",
      "host": "smtp.sendgrid.net",
      "port": 587,
      "use_tls": true,
      "use_ssl": false,
      "host_required": false,
      "kind": "RELAY",
      "username_hint": "Enter the literal word: apikey",
      "password_hint": "Your SendGrid API key",
      "warning": null
    },
    {
      "value": "HOSTINGER",
      "label": "Hostinger Email",
      "host": "smtp.hostinger.com",
      "port": 465,
      "use_tls": false,
      "use_ssl": true,
      "host_required": false,
      "kind": "MAILBOX",
      "username_hint": "Your full email address, e.g. bookings@yourdomain.com",
      "password_hint": "Your mailbox password",
      "warning": "This is a personal or business mailbox, not a bulk sending service. Your provider limits how many messages you can send per day, and that limit varies by plan. For campaigns or high booking volume, use a transactional provider such as SendGrid, Amazon SES, or Postmark."
    },
    {
      "value": "OUTLOOK",
      "label": "Microsoft 365 / Outlook",
      "host": "smtp.office365.com",
      "port": 587,
      "use_tls": true,
      "use_ssl": false,
      "host_required": false,
      "kind": "MAILBOX",
      "username_hint": "Your mailbox address",
      "password_hint": "Your mailbox password",
      "warning": "Microsoft retired password-based SMTP in September 2025. This will most likely fail — use a transactional provider instead."
    }
  ],
  "errors": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `value` / `label` | string | Standard dropdown pair, matching the convention used across this API. |
| `host` | string \| null | The preset host. `null` when the tenant must supply one. |
| `host_required` | boolean | `true` for `SES` and `CUSTOM`. Drives whether the form shows the host field as mandatory. |
| `username_hint` / `password_hint` | string | Field labels for **this** provider. The single highest-value part of this endpoint. |
| `kind` | string | `RELAY`, `MAILBOX`, or `null` for `CUSTOM`. Lets the form group the dropdown into "Sending services" and "Email accounts" — which is itself the guidance most tenants need. |
| `warning` | string \| null | Shown **before** the tenant fills the form, not after they fail. Non-null for every `MAILBOX` provider (send caps) and for `OUTLOOK` (basic auth retired). |

---

## 7. Admin

`tenant/admin/TenantEmailConfigurationAdmin.py`, extending the existing `TenantBaseAdmin`.

- `list_display`: tenant, host, port, `from_email`, status, `verified_at`
- `readonly_fields`: `status`, `verified_at`, `last_tested_at`, `last_used_at`, `last_error`
- `password` rendered as an **empty** `PasswordInput`; save only when non-empty (same trap as §4.5).
- No admin action that sends mail — keep the send path in one place.

---

## 8. Settings

`config/settings/base.py`:

```python
TENANT_SMTP_ENABLED = env.bool("TENANT_SMTP_ENABLED", default=True)
TENANT_SMTP_TIMEOUT_SECONDS = env.int("TENANT_SMTP_TIMEOUT_SECONDS", default=10)
TENANT_SMTP_ALLOWED_PORTS = env.list("TENANT_SMTP_ALLOWED_PORTS", cast=int,
                                     default=[465, 587, 2525])   # 25 excluded — see §4.2
TENANT_SMTP_ALLOW_PRIVATE_HOSTS = env.bool("TENANT_SMTP_ALLOW_PRIVATE_HOSTS", default=False)
APP_ENCRYPTION_KEY = env.str("APP_ENCRYPTION_KEY", default="") or SCHEDULING_TOKEN_ENCRYPTION_KEY
```

Add `REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["smtp_test"] = "5/hour"`.

`config/settings/production.py`: hard-code `TENANT_SMTP_ALLOW_PRIVATE_HOSTS = False` (§4.4).

Migration: `tenant/migrations/0007_tenantemailconfiguration.py`.

---

## 9. Tests

`tenant/tests/test_email_configuration.py`. Run with
`--settings=config.settings.test_postgres` — the default SQLite test DB is broken for this repo.
Remember model tests must exclude `created_by`/`updated_by`/`deleted_by` from `full_clean()`.

| # | Test | Asserts |
|---|---|---|
| T1 | Password round-trips through `set_password`/`get_password` | Encryption works |
| T2 | The stored `password` column is not the plaintext | It is genuinely encrypted at rest |
| T3 | `GET` never returns `password` in any form | §4.5 |
| T4 | `PATCH` omitting `password` preserves the stored value | §4.5 — the easy bug |
| T5 | `host=127.0.0.1` / `169.254.169.254` / `10.0.0.1` → 400 | §4.1 |
| T6 | A hostname resolving to a private IP → 400 | §4.1, not just literal-IP filtering |
| T7 | `port=6379` → 400 | §4.2 |
| T8 | `use_tls=true, use_ssl=true` → 400, not 500 | §4.7 |
| T9 | A tenant can hold several configurations; a second `POST` adds rather than replacing | §3.0 |
| T10 | Another tenant's config is invisible to `GET`, and cannot be promoted via `set-default/` | Isolation |
| T26 | The **first** config a tenant creates comes back `is_default: true`; the second does not | §3.0 |
| T27 | `set-default/` promotes the target **and** demotes the previous default — exactly one remains | §3.0 |
| T28 | Deleting the default promotes another, preferring a `VERIFIED` active one | §3.0 |
| T29 | Deleting the **last** config leaves no default, and the resolver returns the platform sender | §3.0 — the correct resting state |
| T30 | `is_default` in a `POST`/`PATCH` body is ignored, not honoured | §6 — read-only |
| T31 | A duplicate `name` within a tenant → 400; the same name in a different tenant is fine | §3 constraint |
| T32 | The resolver picks the **default**, not the most recently updated or the only verified one | §5.1 |
| T33 | A non-default config can be tested without disturbing the default's `status` | §6 — safe cutover |
| T11 | `UNVERIFIED` config → resolver returns the **platform** sender | §5.1 |
| T12 | `VERIFIED` + `is_active` → resolver returns tenant `connection_kwargs` | §5.1 |
| T13 | Signup OTP and password reset still use platform creds with a `VERIFIED` tenant config present | §1 hard rule |
| T14 | Test-send success flips `UNVERIFIED` → `VERIFIED` and sets `verified_at` | §6.3 |
| T15 | Test-send failure surfaces the SMTP error and leaves status unchanged | §6.3 |
| T16 | 6th test send in an hour → 429 | §4.6 |
| T17 | Send-time host re-validation rejects a rebound host and marks `FAILED` | §4.1 — the one that matters |
| T18 | `provider=SENDGRID` with no host/port/TLS materialises `smtp.sendgrid.net:587` STARTTLS onto the row | §3.1 |
| T19 | `provider=SENDGRID, port=2525` stores **2525** | §3.1 — explicit beats preset |
| T20 | `provider=SES` or `CUSTOM` with no `host` → 400 naming the field | §3.1 |
| T21 | A 535 from `smtp.office365.com` returns the named M365 message, not the raw SMTP error — and does so for a `CUSTOM` config too | §3.1, §5.3 — keyed on host, not preset |
| T22 | `GET /providers/` returns every choice with hints; every `MAILBOX`-kind entry carries a non-null `warning` | §6.4 — note `SES` is a `RELAY` that also warns, about sandbox mode, so "relays never warn" is not the rule |
| T25 | `HOSTINGER` and `ZOHO` materialise port `465` with `use_ssl=True, use_tls=False` | §3.1 — the SSL presets, where getting the flag pair backwards is a `ValueError` from inside the mail backend |
| T23 | `port=25` → 400 | §4.2 |
| T24 | Mutating the preset dict does **not** change an already-saved config's effective host | §3.1 — presets are not a runtime lookup |

Per the standing instruction: **write the tests, do not run the suite after each piece.** Run it once
at the end of Way 3.

---

## 10. The ways

Each way is independently mergeable and leaves the system working. Do not start a way until the
previous one's acceptance criteria pass.

### Way 1 — Storage & security

Model (including `Provider`), `utils/mail/providers.py`, encryption accessors,
`utils/mail/validators.py`, migration `0007`, admin, settings, tests T1–T9, T23.

**Acceptance:** a config can be created in the shell and read back with its password intact; the
column holds ciphertext; every §4 rejection returns a clean `ValidationError`. **No mail is sent by
any new code.**

### Way 2 — The mailer

`utils/mail/{__init__,resolver,service}.py`. The resolver and its fallback ladder, connection
construction with the mandatory timeout, failure classification. Tests T11, T12, T17.

**Acceptance:** `mail.send(..., tenant=t)` delivers through a tenant's config in a test using a fake
SMTP backend, and falls back to platform creds for every condition in the §5.1 table. **Still no
existing caller is modified** — this way adds a capability nothing uses yet.

### Way 3 — The API

Serializers (including preset materialisation), `TenantEmailConfigurationViewSet`, the `providers/`,
`test/` and `set-default/` actions, the automatic promotion rules from §3.0,
`tenant/urls/email_configuration.py`, throttle, and
`docs/apiDocs/tenant/email_configuration.md` at the standard used by the other 40 docs — `*`-marked
field table, per-field validations, endpoint description, exhaustive response cases. Tests T10,
T13–T16, T18–T22, T24.

**Acceptance:** a tenant admin can pick SendGrid from a dropdown, see "Enter the literal word:
apikey" on the username field, paste one API key, test it, and see `VERIFIED` — without typing a
hostname or knowing a port. Failures name the actual cause. Full suite green. **The feature is now
shippable and complete on its own** — tenants can configure SMTP, and nothing in the product routes
through it yet.

### Way 4 — Consumers *(deferred — decide after Way 3 ships)*

Not planned here on purpose. The open questions in §11 are cheap to answer once Ways 1–3 exist and
expensive to answer now.

---

## 11. Open decisions for Way 4

Deliberately unresolved. Each needs a product answer, not a technical one.

1. **Which mail becomes tenant-sent?** Booking notifications are the obvious first. Campaign mail,
   contact-import summaries, and in-app notifications are separate calls.
2. ~~**When a tenant's SMTP fails permanently mid-send, does the message fall back to platform
   creds?**~~ **RESOLVED: no fallback, ever.** The message is not sent, the configuration is marked
   `FAILED`, and the `NotificationLog` row records why. A confirmation from an address the customer
   has never heard of was judged worse for the tenant than a missing one, and the shared-cap problem
   that started this whole effort comes straight back the moment fallback exists.

   **The gap this leaves is #2 in [`settings.md`](apiDocs/tenant/settings.md#known-gaps): nothing
   tells the tenant their sender broke.** Today they find out from a customer. Alerting on the
   transition to `FAILED` — push to tenant admins, reusing `core.services.push.notify_tenant_admins` —
   is the obvious next piece of work and is not built.
3. **Should `.ics` `ORGANIZER` follow the tenant's `from_email`?** It should — problem #2 in §0 is
   exactly this mismatch — but changing `ORGANIZER` on an existing `UID` mid-flight affects bookings
   already sitting in customers' calendars. Needs a cutover plan.
4. **Per-tenant send rate limiting.** Once tenants use their own credentials our shared cap stops
   protecting us from a tenant using our infrastructure as a spam relay. A per-tenant hourly ceiling
   should land with or before Way 4.
5. **SMTP connection reuse.** `send_scheduling_email_task` opens a fresh connection per message. At
   500 reminders that is 500 TLS handshakes against the tenant's relay, and some rate-limit on
   connections rather than messages. Batch by tenant when the reminder beat fans out.

---

## 12. Related, and still worth doing

Moving the **platform** sender off Gmail SMTP to SES or Postmark is a settings change, not an
architecture change — `send_scheduling_email_task` does not care what backend it talks to. It fixes
the §0 cap for every tenant who never configures their own SMTP, which will be most of them. It is
independent of this plan and can ship in parallel.

## Related documents

- [`apiDocs/core/booking.md`](apiDocs/core/booking.md) — the notification lifecycle this will
  eventually serve
- [`apiDocs/core/calendar_integration.md`](apiDocs/core/calendar_integration.md) — the encrypted-
  credential pattern being reused
- [`flows/booking-flow-internals.md`](flows/booking-flow-internals.md) — where the four booking
  emails fire
