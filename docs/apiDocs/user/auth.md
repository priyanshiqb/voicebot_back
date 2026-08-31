# Authentication API Documentation

How a caller proves who they are. Three separate credential types reach this backend, and they are
not interchangeable:

| Credential | Header | Who uses it | Where it comes from |
|---|---|---|---|
| **JWT** | `Authorization: Bearer <access>` | Humans in the dashboard and mobile app | [Login](#1-login) |
| **Agent token** | `Authorization: Bearer vb_<prefix>_<secret>` | The voicebot runtime | Issued in the Django admin |
| **ETL token** | `Authorization: Bearer <static token>` | The knowledge-base ingestion service | `ETL_SYSTEM_TOKEN` setting |

**Base path:** `/api/v1/auth/`

Four things shape this API:

- **Every JWT is backed by a session row.** An access token is not sufficient on its own — its `jti`
  is checked against an active `UserSession` on every request. That is what makes logout and remote
  revocation immediate rather than "wait for the token to expire".
- **Refresh tokens rotate.** Each refresh returns a *new* refresh token and blacklists the old one.
  A refresh token is single-use.
- **Refresh lifetime is per user**, taken from `session_timeout_hours` (default **168** = 7 days),
  not from a global constant.
- **Login is rate-limited by both account and IP.** Five failures locks you out for an hour.

## Endpoints Summary

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/auth/login/` | Exchange email + password for tokens | None |
| `POST` | `/auth/logout/` | Revoke the current session | JWT |
| `POST` | `/auth/refresh/` | Rotate tokens | None (the refresh token is the credential) |
| `POST` | `/auth/forgot-password/` | Send a reset OTP | None |
| `POST` | `/auth/forgot-password/resend/` | Re-send the OTP | None |
| `POST` | `/auth/forgot-password/verify/` | Exchange the OTP for a reset token | None |
| `POST` | `/auth/reset-password/` | Set a new password | None (the reset token is the credential) |

Related: [Users](./users.md) · [Sessions](./sessions.md)

---

## Token lifetimes

| Token | Lifetime | Notes |
|---|---|---|
| Access | **60 minutes** | Fixed. Sent on every authenticated request. |
| Refresh | **`session_timeout_hours`**, default **168 h** (7 days) | Per user, minimum 1 hour. Overrides the `SIMPLE_JWT` default at issue time. |
| Password-reset OTP | **`OTP_VALIDITY_MINUTES`**, default **2 minutes** | 6 digits. |
| Password-reset token | **30 minutes** | Signed, single-use. |
| Agent token | `expires_at`, or none | Set per token in the admin. |

> **Both access *and* refresh are rotated on refresh.** `ROTATE_REFRESH_TOKENS` and
> `BLACKLIST_AFTER_ROTATION` are on, so the refresh token you send is blacklisted and a new one is
> returned. Store the new pair every time — reusing an old refresh token fails.

---

## Endpoint Details

### 1. Login

Exchange credentials for a token pair and open a session.

- **Path**: `POST /api/v1/auth/login/`
- **Auth**: none. Any `Authorization` header is ignored.

| Field | Type | Description | Validation |
|---|---|---|---|
| `email` * | string | The account's email address. | Required, must be a valid email. Compared case-insensitively — emails are lower-cased on save. |
| `password` * | string | The account's password. | Required. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Login successful",
    "data": {
      "access": "eyJhbGciOiJIUzI1NiIs…",
      "refresh": "eyJhbGciOiJIUzI1NiIs…",
      "user_type": "USER",
      "user": {
        "id": "5fa85f64-5717-4562-b3fc-2c963f66afa7",
        "email": "alex@acme.com",
        "first_name": "Alex",
        "last_name": "P"
      }
    },
    "errors": null
  }
  ```

### Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Authenticated. A `UserSession` is opened, recording device name and IP. | `Login successful` | `null` |
| **400** | Wrong email or password. | `Invalid email or password` | `{"credentials": [...]}` |
| **400** | The account exists but `is_active` is false. | `User account is disabled` | `{"account": [...]}` |
| **400** | **The email belongs to an unfinished signup.** See below. | `Your registration is still pending. Please complete your onboarding.` | `{"onboarding_pending": true, "temp_registration_id": "…", "email": "…"}` |
| **400** | `email` malformed or either field missing. | `This field is required.` | `{"<field>": [...]}` |
| **403** | Five failed attempts for this email **or** from this IP. | `Account or IP temporarily locked due to too many failed login attempts. Please try again in 1 hour.` | `null` |
| **503** | Credentials were correct, but the session could not be registered. **No tokens are issued.** Safe to retry. | `Could not start a session. Please try again.` | `null` |

> **Why a correct password can still return `503`.** A login issues tokens *and* writes a
> `UserSession` row; every authenticated request afterwards resolves its access token against
> that row. Returning tokens without one would hand you credentials that 401 on every call —
> "logged in, and nothing works". So the write is part of the login, and its failure is
> reported rather than swallowed. Treat it as a transient failure and retry the login.

> **Handle the onboarding-pending case explicitly.** If the credentials match a `TempUser` who has
> not finished signing up, login fails with a `400` carrying `errors.onboarding_pending: true` and a
> `temp_registration_id`. Send the user back into the
> [signup flow](../onboard/signup_flow.md) with that id rather than showing "invalid password" —
> the password was correct.

> **The lockout is checked before the password is.** Five failures locks the account *and* the
> source IP for **1 hour** (`DEFENDER_LOGIN_LOCKOUT_LIMIT`, `DEFENDER_COOLOFF_TIME`). During the
> lockout even correct credentials return `403`. The counter is keyed on both, so a shared office IP
> can lock out colleagues.

> **`last_login` is updated on every successful login**, before the session is created.

---

### 2. Logout

Revokes the session and blacklists the refresh token.

- **Path**: `POST /api/v1/auth/logout/`
- **Auth**: **JWT required** — unlike login and refresh, this endpoint is authenticated.

| Field | Type | Description | Validation |
|---|---|---|---|
| `refresh` * | string | The refresh token for the session being closed. | Required. Must be a valid, unexpired, non-blacklisted refresh token. |
| `device_token` | string | The FCM registration token of the device signing out. Deactivates it so push notifications stop reaching this device. | Optional. Matched against **your own** tokens only — naming someone else's silently does nothing. Unknown values are ignored. |

| Status | When | `message` |
|---|---|---|
| **200** | Refresh token blacklisted and the matching `UserSession` revoked. | `Logout successful` |
| **400** | `refresh` missing or empty. | `Refresh token is required.` |
| **400** | `refresh` malformed, expired, or already blacklisted. | `Invalid refresh token.` |
| **401** | No valid access token on the request. | `Authentication credentials were not provided.` |

> **Logout revokes the access token too, not just the refresh.** The session's `access_jti` is
> marked revoked in the cache, so the access token is rejected on its very next use rather than
> remaining valid for up to an hour.

> 🔔 **Send `device_token` if you use push.** Without it the device stays subscribed, and on a
> shared browser or a re-issued handset the next alert lands in front of whoever is sitting there —
> notification bodies render on lock screens. Do **both** halves on sign-out: call the Firebase
> SDK's `deleteToken()` *and* pass the token here. Deactivating server-side alone leaves the device
> subscribed at Google.
>
> Deactivation is **best-effort**: if it fails, the logout still succeeds with a `200`. A sign-out
> that broke because push bookkeeping broke would be the worse bug. See
> [Device Tokens](../core/device_token.md).

> **You must send the refresh token even though you are authenticated with the access token.** The
> access token alone does not say which session to close — a user may have several.

> **Logging out one session does not touch the others.** To end every session, use
> [Sessions](./sessions.md).

---

### 3. Refresh

Rotate an expiring access token.

- **Path**: `POST /api/v1/auth/refresh/`
- **Auth**: none — the refresh token in the body *is* the credential.

| Field | Type | Description | Validation |
|---|---|---|---|
| `refresh` * | string | The current refresh token. | Required. Must be valid, unexpired, not blacklisted, and belong to an **active session**. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Token refreshed successfully",
    "data": {
      "access": "eyJhbGciOiJIUzI1NiIs…",
      "refresh": "eyJhbGciOiJIUzI1NiIs…"
    },
    "errors": null
  }
  ```

| Status | When | `message` |
|---|---|---|
| **200** | Rotated. **Both** tokens are new; the old refresh is blacklisted and the old access revoked. | `Token refreshed successfully` |
| **400** | `refresh` missing. | `Refresh token is required.` |
| **400** | `refresh` malformed, expired, or already blacklisted. | `Invalid refresh token.` |
| **400** | The account has been deactivated since login. | `User account is inactive.` |
| **400** | The account has been soft-deleted since login. | `User account is deleted.` |
| **400** | The session was logged out, revoked, or expired. | `Session is invalid or has been logged out.` |
| **400** | The token's user no longer exists. | `User not found.` |
| **503** | Tokens rotated, but the session row could not be updated. **No tokens are returned, and the token you sent is already spent.** Log in again. | `Could not refresh the session. Please log in again.` |

> **Replace both tokens in storage.** The response contains a new `refresh` as well as a new
> `access`. Keeping the old refresh token guarantees the next refresh fails.

> **No failure here is a `401`.** Unusual for an auth endpoint — do not branch on `401` to detect
> an expired session. Everything recoverable is a `400`; the one `503` is not recoverable and means
> "start a fresh login".

> ⚠️ **Do not refresh concurrently.** Rotation invalidates the token you sent, so if two requests
> refresh with the same token the second gets `Invalid refresh token.` and most clients respond by
> logging the user out. This bites hardest with multiple browser tabs, or when several API calls
> `401` at once and each triggers its own refresh.
>
> **Serialise it:** keep one in-flight refresh promise per client and have every other caller await
> it, rather than issuing their own. There is no server-side grace window for a replayed token.

> **The new refresh token's lifetime comes from the user's `session_timeout_hours`**, re-applied on
> each rotation. Changing that value takes effect from the next refresh onwards, not immediately.

---

## Password reset

Four steps. OTP → verify → signed token → new password.

```
POST /forgot-password/          → 6-digit OTP emailed (valid 2 min)
POST /forgot-password/resend/   → new OTP, 30s cooldown
POST /forgot-password/verify/   → returns a signed reset token (valid 30 min)
POST /reset-password/           → sets the new password
```

---

### 4. Forgot Password (step 1)

- **Path**: `POST /api/v1/auth/forgot-password/`
- **Auth**: none.

| Field | Type | Description | Validation |
|---|---|---|---|
| `email` * | string | The account to reset. | Required, valid email. Lower-cased before lookup. |

| Status | When | `message` |
|---|---|---|
| **200** | OTP generated and queued for email — **or** the email is not registered. The response is identical either way. | `If the email is registered, a verification OTP code will be sent shortly.` |
| **400** | An OTP was requested for this account under 30 seconds ago. | `Please wait before requesting another OTP.` |
| **400** | `email` missing or malformed. | `This field is required.` |

The cooldown response carries the remaining time:
```json
{
  "status": false,
  "message": "Please wait before requesting another OTP.",
  "data": { "cooldown_remaining": 18 },
  "errors": ["OTP_COOLDOWN_ACTIVE"]
}
```

> **`errors` is an array of codes here, not the usual field map.** Branch on
> `errors[0] === "OTP_COOLDOWN_ACTIVE"`.

> **The success response deliberately does not reveal whether the email exists** — but see
> [Known gaps](#known-gaps), because the cooldown branch undoes this.

> **Email delivery failures are swallowed.** The OTP is stored and a `200` returned even if queuing
> the email fails. A user who never receives a code may still be inside the cooldown window.

---

### 5. Resend OTP (step 1, repeated)

Identical to step 1, with wording for a second send.

- **Path**: `POST /api/v1/auth/forgot-password/resend/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `email` * | string | The account to resend to. | Required, valid email. |

| Status | When | `message` |
|---|---|---|
| **200** | A new OTP was generated, or the email is unknown. | `A new verification OTP has been emailed.` |
| **400** | Under 30 seconds since the last OTP. Carries `cooldown_remaining` and `["OTP_COOLDOWN_ACTIVE"]`. | `Please wait before resending OTP.` |
| **400** | `email` missing or malformed. | `This field is required.` |

> **Resending invalidates the previous OTP** — the stored code is overwritten, so only the newest
> email works.

---

### 6. Verify OTP (step 2)

Exchange the emailed code for a signed reset token.

- **Path**: `POST /api/v1/auth/forgot-password/verify/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `email` * | string | The account being reset. | Required, valid email. |
| `otp_code` * | string | The 6-digit code from the email. | Required. **5–6 characters** — the field accepts 5, though issued codes are always 6. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "OTP verified successfully.",
    "data": { "token": "eyJ1c2VyX2lkIjoi…:1abcDe:xY…" },
    "errors": null
  }
  ```

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Correct and unexpired. The OTP is cleared and a 30-minute reset token returned. | `OTP verified successfully.` | `null` |
| **400** | Wrong code, or the email is not registered. **Indistinguishable by design.** | `Invalid OTP code.` | `["INVALID_OTP"]` |
| **400** | Correct code, but older than `OTP_VALIDITY_MINUTES` (default 2). | `Verification code has expired.` | `["OTP_EXPIRED"]` |
| **400** | Five wrong attempts within 10 minutes. **The OTP is destroyed** — request a new one. | `Too many failed attempts. Verification code has been invalidated.` | `["TOO_MANY_ATTEMPTS"]` |
| **400** | `email` or `otp_code` missing or malformed. | `This field is required.` | field map |

> **Attempts are counted per email over a 10-minute window, and unknown emails are counted too** —
> against a hashed key — so probing for valid addresses looks exactly like mistyping a code.

> **A used OTP reads as `INVALID_OTP`, not `OTP_EXPIRED`.** Verification clears the stored code, so
> submitting the same code twice gives the generic invalid message.

---

### 7. Reset Password (step 3)

- **Path**: `POST /api/v1/auth/reset-password/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `email` * | string | The account being reset. | Required, valid email. Must match the token's owner. |
| `token` * | string | The signed token from step 2. | Required. Valid signature, under 30 minutes old, and **not already used**. |
| `password` * | string | The new password. | Required. **8–32 characters**. No spaces. At least one uppercase, one lowercase, one digit, and one of `!@#$%^&*(),.?":{}\|<>`. |
| `confirm_password` * | string | Repeat of `password`. | Required. Must match exactly. |

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Password changed. | `Password reset successfully.` | `null` |
| **400** | No active account for that email. | `Invalid email or user not found.` | `["USER_NOT_FOUND"]` |
| **400** | The token is more than 30 minutes old. | `Reset token has expired.` | `["TOKEN_EXPIRED"]` |
| **400** | The token was tampered with or is not ours. | `Invalid token signature.` | `["INVALID_TOKEN"]` |
| **400** | The token belongs to a different account than `email`. | `Token is not associated with this user.` | `["INVALID_USER_TOKEN"]` |
| **400** | The token was already used to change a password. | `This reset link has already been used.` | `["TOKEN_ALREADY_USED"]` |
| **400** | Password too short/long, missing a character class, or containing a space. | e.g. `Password must contain at least one uppercase letter.` | `{"password": [...]}` |
| **400** | `password` and `confirm_password` differ. | `Passwords do not match.` | `{"confirm_password": "…"}` |

> **Single use is enforced by embedding the old password hash in the token.** Once the password
> changes the hash no longer matches, so the token is spent. There is no separate revocation list.

> **Resetting a password does NOT log other sessions out.** Existing access and refresh tokens stay
> valid — see [Known gaps](#known-gaps). Revoke them explicitly via [Sessions](./sessions.md) if
> the reset was prompted by a compromise.

---

## 8. Voicebot Agent Tokens

Long-lived machine credentials for the voicebot runtime. Issued in the Django admin, never through
this API.

**Format:** `vb_<prefix>_<secret>` — e.g. `vb_a1b2c3_s3cr3tstring`.

```http
Authorization: Bearer vb_a1b2c3_s3cr3tstring
```

Only the prefix is stored in plain text; the secret is hashed. **The secret is shown once at
creation and cannot be recovered** — reissue the token if it is lost.

**How a token is validated, in order:**

1. The header must start with `Bearer` and the token with `vb_`. Anything else is passed to the JWT
   authenticator rather than failing.
2. The prefix must match an `is_active` token.
3. The secret must match the stored hash.
4. `expires_at`, if set, must be in the future.
5. The owning user must be `is_active` and of type `VB_AGENT`.

| Status | When | `message` |
|---|---|---|
| **401** | No token matches the prefix, or the token is inactive. | `Invalid or inactive agent token.` |
| **401** | Prefix matched but the secret is wrong. | `Invalid agent token secret.` |
| **401** | Past `expires_at`. | `Agent token has expired.` |
| **401** | The owner is deactivated or is not a `VB_AGENT`. | `User is inactive or not an agent.` |

> **Agent tokens carry no tenant.** Every endpoint they can reach requires an `X-Tenant-ID` header
> naming the tenant, and that header is a **filter, not an authorisation boundary** — a valid token
> can name any tenant. Treat these as high-value secrets: store them in a secret manager, rotate
> them, and never expose one to a browser. Full rationale:
> [Tenant scoping for VB_AGENT callers](../core/vb_agent_tenant_scoping.md).

> **What an agent token may call is an explicit allowlist per endpoint**, closed by default. See the
> same document for the current surface.

> **`last_used_at` is written on every authenticated request**, so each agent call costs one extra
> database write.

---

## 9. ETL System Token

A single static bearer token for the knowledge-base ingestion service, configured as
`ETL_SYSTEM_TOKEN`.

```http
Authorization: Bearer <ETL_SYSTEM_TOKEN>
```

It resolves to the one active user of type `ETL` and reaches exactly one endpoint:
[`update-etl-status`](../core/knowledge_base.md#13-update-etl-status-etl-service-only).

| Status | When | `message` |
|---|---|---|
| **401** | The token does not match `ETL_SYSTEM_TOKEN`. | `Invalid ETL system token.` |
| **401** | No active `ETL` user exists in the database. | `System ETL user not found in database.` |

> **ETL callers are not tenant-scoped**, by design — the service ingests every tenant's knowledge
> bases. The token is as sensitive as an agent token.

> **The default is a placeholder.** `ETL_SYSTEM_TOKEN` falls back to
> `dummy_etl_system_token_for_local_dev` when unset, and that value authenticates successfully. Set
> it explicitly in every deployed environment.

---

## How authentication is selected

Both authenticators run in order on every request:

1. **`AgentTokenAuthentication`** — claims the request only if the token starts with `vb_`.
   Otherwise it returns `None` and passes it along.
2. **`CachedJWTAuthentication`** — validates the JWT signature *and* checks the token's `jti`
   against an active `UserSession`.

> **A structurally valid JWT is still rejected if its session is gone.** Signature and expiry are
> not enough: logout, an admin revoking the session, or session expiry all produce
> `Session is invalid or logged out.` This is what makes revocation immediate.

> **Session validation is cached in Redis and falls back to the database when Redis is down.**
> A revoked token is cached as revoked for the remainder of its lifetime; an active session's
> `last_used` is written at most once per 60 seconds to avoid a write on every request.

## Email delivery records

Every platform email — signup OTP, email-change verification, password reset, and their resends —
writes a row to `user.EmailDeliveryLog`. There is **no API for it**; it is queried from the admin,
the shell, or a support tool.

> **The admin view is superuser-only, and read-only.** Every other log in the admin is scoped by a
> `tenant` ForeignKey; this model has none — `tenant_id` is a bare UUID so a row outlives the tenant
> it describes — so that scoping cannot apply here. What is left spans every tenant plus pre-signup
> `TempUser` records and holds addresses and request IPs, which is more than `is_staff` should carry.
> Rows cannot be added or edited: an editable audit trail is not one. Deletion stays available
> (erasure requests arrive sooner than the retention window) and is a **hard** delete, matching the
> purge task — a soft delete would hide the row while leaving the address and IP in the table.

| Column | Notes |
|---|---|
| `purpose` | `SIGNUP_OTP`, `EMAIL_CHANGE`, `PASSWORD_RESET`, `SMTP_TEST` |
| `recipient` | Always set — the only identifier guaranteed to exist |
| `user` | The account, when there is one. Null during signup. |
| `temp_user_id` | The in-progress signup. **Not redundant with `recipient`** — see below. |
| `tenant_id` | When one existed |
| `status`, `sent_at`, `error` | Delivery outcome |
| `ip_address`, `user_agent` | Who requested it — the point of logging resets at all |

> **`temp_user_id` is what survives an email change.** Changing the address mid-signup rewrites
> `TempUser.email` **in place**, so the messages before and after land on different addresses.
> Hijacking an in-progress signup by swapping its email is the attack this log exists to make
> visible, so a trail that split in half at that moment would be worse than none.

> **The OTP code and reset token are never stored.** Purpose, recipient, outcome, requester — nothing
> more. A table of live one-time codes would be a credential store and a worse liability than the
> visibility it buys. Subjects and bodies are excluded for the same reason.

> **An unknown email address leaves no row.** Password reset answers identically whether or not an
> account exists; the log must not become the oracle that undoes that.

> **Recording can never block a send.** Both the insert and the outcome update swallow their errors —
> an audit trail that can stop a password reset is worse than no audit trail.

### Retention

Purged daily by `user.tasks.purge_email_delivery_logs`. Rows hold an email address and the
requester's IP, so this is a privacy obligation before a disk one.

| Purpose | Kept | Why |
|---|---|---|
| `PASSWORD_RESET` | **365 days** | Security-audit evidence, reached for long after the event |
| `EMAIL_CHANGE` | **365 days** | Account-takeover vector |
| `SIGNUP_OTP` | **180 days** | Support data, shorter useful life |
| `SMTP_TEST` | **90 days** | Operational noise |

A purpose with no configured window falls back to 180 days — erring short, so an unclassified record
expires rather than accumulating forever because a settings line was missed.

> **Unlike `NotificationLog`, deleting a row here is simply forgetting.** Booking-notification rows
> *are* the idempotency mechanism, so purging one re-arms it; that is why the two live in separate
> tables with separate purge tasks.

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **A password reset does not revoke existing sessions.** | After a reset, previously issued access and refresh tokens keep working until they expire — including any an attacker holds. Revoke sessions manually if the reset followed a compromise. |
| 2 | **The OTP cooldown leaks whether an email is registered.** Steps 4 and 5 return `200` for unknown addresses, but a registered address inside the 30-second window returns `400 OTP_COOLDOWN_ACTIVE`. Two quick requests distinguish the two. | Undermines the enumeration protection the identical success messages were written to provide. |
| 3 | **`reset-password` returns `USER_NOT_FOUND` for unknown emails.** | The same leak, directly. |
| 4 | **A login whose session row fails to save still returns tokens.** The failure is logged and swallowed, but `CachedJWTAuthentication` then rejects the access token because no session exists. | The client receives a `200` with tokens that fail on first use. |
| 5 | **`never_expires` on an agent token is not honoured by the authenticator.** It checks only `expires_at`, so a token flagged never-expiring but carrying a date still expires. | Contradicts the field's own help text. |
| 6 | **`IsWithinLoginWindow` calls `user.can_login_now()`, which is not defined on the model** — and the class is imported but never registered in any `permission_classes`. | Dead code today; enabling it would raise `AttributeError` and return `500` on every request. |

## Related

- [Users API](./users.md) — accounts, roles, `session_timeout_hours`
- [Sessions API](./sessions.md) — listing and revoking active sessions
- [Signup flow](../onboard/signup_flow.md) — where `onboarding_pending` sends the user
- [Tenant scoping for VB_AGENT callers](../core/vb_agent_tenant_scoping.md)
