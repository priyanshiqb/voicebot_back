# Signup Flow API Documentation

How a stranger becomes a tenant. Signup is a **staged flow against a throwaway `TempUser` record** —
nothing lands in the real user table until payment succeeds, at which point the account, the
tenant, and the subscription are all created together.

**Base path:** `/api/v1/onboard/`

Five things govern this flow:

- **A `TempUser` is not a user.** It holds the details and a hashed password, but cannot log in.
  Attempting to [log in](../user/auth.md#1-login) with those credentials returns a `400` carrying
  `onboarding_pending` and the `temp_registration_id` — send them back here.
- **Two different credentials are used.** Steps 1–2 are anonymous and identified by
  `temp_registration_id`. From step 3 onwards you send a **temporary token** as a bearer token.
- **The flow is resumable.** [`resume-session`](#7-resume-a-session) works out how far someone got
  and tells you which step to render.
- **Free plans skip the payment gateway entirely.** A plan totalling zero completes onboarding
  inside `create-order-just-in-time` and returns real JWTs there.
- **Nothing is guaranteed to be cleaned up.** An abandoned `TempUser` holds its email and phone
  number indefinitely — see [Known gaps](#known-gaps).

## The flow

```
POST /check-email/                    (optional) is this email free?
POST /check-phone/                    (optional) is this phone free?
POST /register-pending/               → temp_registration_id, OTP emailed
POST /verify-otp/                     → temporary_token
   ├─ POST /resend-otp/               didn't arrive
   └─ POST /change-email/             wrong address
POST /update-plan/                    (optional) change plan   [temp token]
POST /create-order-just-in-time/      billing address → Razorpay order   [temp token]
   └─ free plan? → onboarding completes here, returns JWTs
POST /verify/                         payment signature → JWTs   [temp token]
   └─ POST /cancel-order/             abandon this order   [temp token]
```

Resuming: `POST /resume-session/` (email + password) or `POST /resume-session-token/` (temp token).

## Endpoints Summary

| Method | Path | Purpose | Auth |
|---|---|---|---|
| `POST` | `/onboard/check-email/` | Is this email available? | None |
| `POST` | `/onboard/check-phone/` | Is this phone available? | None |
| `POST` | `/onboard/register-pending/` | Start signup, email an OTP | None |
| `POST` | `/onboard/verify-otp/` | Verify the OTP, get a temp token | None |
| `POST` | `/onboard/resend-otp/` | Re-send the signup OTP | None |
| `POST` | `/onboard/change-email/` | Fix a mistyped email, re-send OTP | None |
| `POST` | `/onboard/resume-session/` | Resume with email + password | None |
| `POST` | `/onboard/resume-session-token/` | Resume with a temp token | Temp token |
| `PATCH` | `/onboard/update-plan/` | Switch plan before paying | Temp token |
| `POST` | `/onboard/create-order-just-in-time/` | Save billing, create the order | Temp token |
| `POST` | `/onboard/verify/` | Verify payment, finish onboarding | Temp token |
| `POST` | `/onboard/cancel-order/` | Cancel a pending order | Temp token |

Password reset for **unfinished** signups is a separate flow — see
[password_reset](./password_reset.md). The gateway callback is [webhooks](./webhooks.md).

---

## The temporary token

Issued by [`verify-otp`](#4-verify-the-otp) and [`resume-session`](#7-resume-a-session), sent as:

```http
Authorization: Bearer <temporary_token>
```

| Property | Value |
|---|---|
| Lifetime | **7 days** |
| Contents | The `TempUser` id, cryptographically signed |
| Scope | Only the onboarding endpoints marked "Temp token" above |

| Status | When | `message` |
|---|---|---|
| **401** | The token is older than 7 days. | `Temporary token has expired` |
| **401** | Signature invalid, or the `TempUser` no longer exists. | `Invalid temporary token` |
| **403** | A valid **JWT** was sent instead of a temp token. | permission denied |

> **It is not a JWT and is not interchangeable with one.** A real access token fails these
> endpoints, and a temp token fails everything else.

> **It survives longer than the OTP by design** — 7 days versus 2 minutes — so a user who verified
> their email can come back later and finish paying without re-verifying.

---

## Endpoint Details

### 1. Check Email Availability

Non-committal lookup for live form feedback.

- **Path**: `POST /api/v1/onboard/check-email/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `email` * | string | The address to test. | Required, valid email. Trimmed and lower-cased. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Email availability checked (fast-path).",
    "data": {
      "email": "jane@example.com",
      "available": true,
      "exists_in_active_users": false,
      "exists_in_pending_registrations": false
    },
    "errors": null
  }
  ```

| Field | Meaning |
|---|---|
| `available` | `true` when the address is free in **both** tables. |
| `exists_in_active_users` | Taken by a real account — including **soft-deleted** ones, which still reserve the address. |
| `exists_in_pending_registrations` | Taken by an unfinished signup. |

| Status | When | `message` |
|---|---|---|
| **200** | Checked. | `Email availability checked (fast-path).` or `(slow-path).` |
| **400** | `email` missing or malformed. | `Validation failed` |

> **The `message` tells you which path answered.** A Bloom filter answers "definitely free"
> instantly (`fast-path`); anything it flags is confirmed against the database (`slow-path`). The
> filter can produce false positives but never false negatives, so `available: true` is always
> trustworthy.

> **Advisory only.** Someone else can take the address between this call and
> [register-pending](#3-start-the-signup), which returns its own `400`.

---

### 2. Check Phone Availability

Same idea for phone numbers. **No Bloom filter** — this always hits the database.

- **Path**: `POST /api/v1/onboard/check-phone/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `phone_number` * | string | The number to test. | Required. Trimmed. **Compared as a raw string** — no normalisation, so `+91 98765 43210` and `919876543210` are different numbers here. |

- **Success Response (200 OK)**: as §1, with `phone_number` in place of `email`.

| Status | When | `message` |
|---|---|---|
| **200** | Checked. | `Phone number availability checked.` |
| **400** | `phone_number` missing. | `Validation failed` |

> **Exact-string matching makes this weaker than it looks.** A number already registered in a
> different format reports as available, and the real conflict only surfaces at
> [create-order](#10-create-the-order). Send the number in exactly the format you will submit later.

---

### 3. Start the Signup

Creates the `TempUser` and emails a 6-digit OTP.

- **Path**: `POST /api/v1/onboard/register-pending/`
- **Request Payload**:
  ```json
  {
    "plan_id": "1a2b3c4d-…",
    "first_name": "Jane",
    "last_name": "Doe",
    "business_name": "Acme Robotics",
    "email": "jane@example.com",
    "password": "Str0ng!Pass",
    "confirm_password": "Str0ng!Pass",
    "referral_code": "PARTNER10",
    "timezone_id": "3fa85f64-…",
    "language_id": "7bf26cf4-…"
  }
  ```

### Request fields

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `plan_id` * | UUID | The subscription plan chosen on the pricing page. | Required. Must be an existing **active** [plan](./plans.md). |
| `first_name` * | string | Given name. | Required. **2–30** characters, **letters only** — no spaces, digits, hyphens, or apostrophes. `O'Brien` and `Mary Jane` are rejected. |
| `last_name` * | string | Family name. | Required. Same rules as `first_name`. |
| `business_name` * | string | The organisation's name. Becomes the tenant name, and seeds its subdomain. | Required. **2–255** characters. Any characters allowed. |
| `email` * | string | Sign-in address. | Required. **6–100** characters, valid email. Must not exist as an active user **or** a pending registration. Lower-cased. |
| `password` * | string | The eventual account password. | Required. **8–32** characters, no spaces, at least one uppercase, one lowercase, one digit, and one of `!@#$%^&*(),.?":{}\|<>`. |
| `confirm_password` * | string | Repeat of `password`. | Required. Must match exactly. |
| `referral_code` | string | Partner or referral code, recorded on the tenant as `referred_by_code`. | Optional, may be blank or null. Max **50**. **Not validated** against anything — any string is accepted and stored. |
| `timezone_id` | UUID | Preferred [timezone](./timezones.md). | Optional, nullable. Must exist. Defaults to the system default when omitted. |
| `language_id` | UUID | Preferred [language](./languages.md). | Optional, nullable. Must exist. Defaults to the system default when omitted. |

- **Success Response (201 Created)**:
  ```json
  {
    "status": true,
    "message": "Registration initiated. Verification OTP has been emailed.",
    "data": {
      "temp_registration_id": "9f1c…",
      "email": "jane@example.com"
    },
    "errors": null
  }
  ```

**Store `temp_registration_id`** — steps 4–6 need it, and it is what the login endpoint hands back
if the user tries to sign in before finishing.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **201** | Registration started, OTP emailed. | `Registration initiated. Verification OTP has been emailed.` | `null` |
| **400** | Email already registered. | `Validation failed` | `{"email": ["A user with this email already exists."]}` |
| **400** | Email already has a pending signup. | `Validation failed` | `{"email": ["A registration is already pending for this email."]}` |
| **400** | A name contains anything but letters. | `Validation failed` | `{"first_name": ["First name must contain only alphabetic characters."]}` |
| **400** | Password fails a complexity rule. | `Validation failed` | `{"password": [...]}` |
| **400** | Passwords do not match. | `Validation failed` | `{"confirm_password": "Passwords do not match."}` |
| **400** | `plan_id` unknown or inactive. | `Validation failed` | `{"plan_id": ["Selected plan is invalid or inactive."]}` |
| **400** | `timezone_id` / `language_id` unknown. | `Validation failed` | `{"<field>": ["Selected timezone is invalid."]}` |

> **`errors` is a field map here, and `message` is always the literal string `Validation failed`.**
> Unlike most of this API, `message` carries no detail — read `errors`.

> **The name rules are stricter than real names.** Letters only, so hyphenated and apostrophised
> names, and any name with a space, cannot complete signup. Worth knowing before you blame the
> user.

> **The OTP email is best-effort.** If queuing fails the registration still returns `201`; in
> non-production the code is printed to the server log.

---

### 4. Verify the OTP

Confirms the email and issues the temporary token.

- **Path**: `POST /api/v1/onboard/verify-otp/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `temp_registration_id` * | UUID | From step 3. | Required. Must match a pending registration. |
| `otp_code` * | string | The 6-digit code from the email. | Required. Compared exactly as a string. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "OTP verified successfully.",
    "data": {
      "temporary_token": "eyJ0ZW1wX3VzZXJfaWQiOi…",
      "email": "jane@example.com",
      "plan": { "id": "1a2b…", "name": "Pro", "code": "PRO" },
      "billing_breakdown": {
        "base_price": "4000.00",
        "discount_amount": "0.00",
        "net_base_price": "4000.00",
        "tax_percentage": "18.00",
        "tax_amount": "720.00",
        "total_price": "4720.00",
        "currency": "INR"
      }
    },
    "errors": null
  }
  ```

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Verified. Email marked confirmed and a 7-day token issued. | `OTP verified successfully.` | `null` |
| **400** | `temp_registration_id` unknown or malformed. | `Invalid registration ID` | `["REGISTRATION_NOT_FOUND"]` |
| **400** | Wrong code. | `Invalid OTP code` | `["INVALID_OTP"]` |
| **400** | Correct code, but older than `OTP_VALIDITY_MINUTES` (default **2**). | `Verification code has expired` | `["OTP_EXPIRED"]` |
| **400** | Five wrong attempts within 10 minutes. **The OTP is destroyed** — use [resend](#5-resend-the-otp). | `Too many failed attempts. Verification code has been invalidated.` | `["TOO_MANY_ATTEMPTS"]` |

> **`errors` is an array of codes on this endpoint**, not a field map. Branch on `errors[0]`.

> **All prices are strings, not numbers**, to avoid float rounding. Parse them as decimals.

> **Verifying twice fails.** The OTP is cleared on success, so a second attempt returns
> `INVALID_OTP`. Use [resume-session](#7-resume-a-session) to get another token instead.

---

### 5. Resend the OTP

- **Path**: `POST /api/v1/onboard/resend-otp/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `temp_registration_id` * | UUID | From step 3. | Required. Must match a pending registration. |

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | New OTP generated and emailed. | `A new verification OTP has been emailed.` | `null` |
| **400** | Unknown registration id. | `Invalid registration ID` | `["REGISTRATION_NOT_FOUND"]` |
| **400** | This email is already verified — there is nothing to resend. | `Email is already verified` | `["EMAIL_ALREADY_VERIFIED"]` |
| **400** | Under 30 seconds since the last OTP. `data.cooldown_remaining` holds the seconds left. | `Please wait before resending OTP.` | `["OTP_COOLDOWN_ACTIVE"]` |

> **Resending invalidates the previous code** and restarts both the 2-minute validity and the
> 30-second cooldown.

> **The brute-force counter is not reset by a resend.** If the user already burned five attempts,
> a fresh code still returns `TOO_MANY_ATTEMPTS` until the 10-minute window elapses.

---

### 6. Change the Email

For a mistyped address, before verification.

- **Path**: `POST /api/v1/onboard/change-email/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `temp_registration_id` * | UUID | From step 3. | Required. Must match a pending, **unverified** registration. |
| `new_email` * | string | The corrected address. | Required, valid email. Must not exist as an active user or another pending registration. |

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Email replaced and a fresh OTP sent to the new address. | `Email updated successfully. A new verification OTP has been emailed.` | `null` |
| **400** | Unknown registration id. | `Invalid registration ID` | `["REGISTRATION_NOT_FOUND"]` |
| **400** | Already verified — the address is now fixed. | `Email is already verified` | `["EMAIL_ALREADY_VERIFIED"]` |
| **400** | Taken by an active account. | `Validation failed` | `{"new_email": ["User account with this email address already exists."]}` |
| **400** | Taken by another pending signup. | `Validation failed` | `{"new_email": ["A pending registration already exists for this email address."]}` |

> **No cooldown applies here**, so this is also the escape hatch when a user is stuck behind the
> resend cooldown and is willing to re-enter their address.

> **Only available before verification.** After that the address is locked for the rest of signup.

---

### 7. Resume a Session

Works out how far someone got and what to show next. Two variants.

**With email and password** — `POST /api/v1/onboard/resume-session/`, no auth:

| Field | Type | Description | Validation |
|---|---|---|---|
| `email` | string | The registration's email. | Provide this **or** `temp_registration_id`. |
| `temp_registration_id` | UUID | The registration id. | Takes precedence over `email` when both are sent. |
| `password` * | string | The password chosen at step 3. | Required. Checked against the stored hash. |

**With a temp token** — `POST /api/v1/onboard/resume-session-token/`, temp token required, no body.
Use this when you still hold a valid token; it skips the password check.

- **Success Response (200 OK)** — email already verified:
  ```json
  {
    "status": true,
    "message": "Session resumed successfully.",
    "data": {
      "temporary_token": "eyJ0ZW1wX3VzZXJfaWQiOi…",
      "email": "jane@example.com",
      "step": "BILLING_ADDRESS_REQUIRED",
      "plan": { "id": "1a2b…", "name": "Pro", "code": "PRO" },
      "billing_breakdown": { "total_price": "4720.00", "currency": "INR" },
      "billing_address": null,
      "razorpay_order_id": null
    },
    "errors": null
  }
  ```

**`step` tells you which screen to render:**

| `step` | Meaning | Go to |
|---|---|---|
| `OTP_VERIFICATION_REQUIRED` | Email not yet verified. A fresh OTP has just been emailed. | [§4](#4-verify-the-otp) |
| `BILLING_ADDRESS_REQUIRED` | Verified, no billing address saved. | [§10](#10-create-the-order) |
| `PROCEED_TO_PAYMENT` | Billing address saved, no pending order. | [§10](#10-create-the-order) |
| `PAYMENT_VERIFICATION_REQUIRED` | An order exists and is awaiting payment. `razorpay_order_id` is populated. | [§11](#11-verify-the-payment) |

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Resumed. Read `step`. | `Session resumed successfully.` / `Registration is pending email verification. A new OTP has been emailed.` | `null` |
| **400** | Password does not match. | `Incorrect password` | `["INVALID_CREDENTIALS"]` |
| **400** | The email belongs to a **completed** account. | `User is already fully registered and onboarded. Please log in.` | `["USER_ALREADY_ONBOARDED"]` |
| **404** | No pending registration for that email or id. | `No active registration session found. Please sign up first.` | `["SESSION_NOT_FOUND"]` |

> **In the unverified case a new OTP is sent immediately, bypassing the 30-second cooldown**, and
> **no `temporary_token` is returned** — the response carries only `temp_registration_id`, `email`,
> and `step`.

> **`USER_ALREADY_ONBOARDED` only appears when you resume by `email`.** Resuming by
> `temp_registration_id` cannot detect it, and returns `SESSION_NOT_FOUND` instead.

---

### 8. Update the Plan

Switch plans before paying.

- **Path**: `PATCH /api/v1/onboard/update-plan/` — note **PATCH**, not POST.
- **Auth**: temp token.

| Field | Type | Description | Validation |
|---|---|---|---|
| `plan_id` * | UUID | The new plan. | Required. Must be an existing **active** plan. |

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Plan changed. The response carries the recalculated `billing_breakdown`. | plan updated | `null` |
| **404** | `plan_id` unknown, inactive, or malformed. | `Plan not found or inactive` | `["PLAN_NOT_FOUND"]` |
| **401** / **403** | Missing, expired, or non-temp token. | see [The temporary token](#the-temporary-token) | — |

> **Changing plans does not cancel an existing order.** If an order was already created for the old
> price, cancel it ([§12](#12-cancel-an-order)) and create a new one, or the user pays the old
> amount.

---

### 10. Create the Order

Saves the billing address and phone number, then creates the payment-gateway order.

- **Path**: `POST /api/v1/onboard/create-order-just-in-time/`
- **Auth**: temp token.
- **Request Payload**:
  ```json
  {
    "phone_number": "919876543211",
    "billing_address": {
      "line1": "123 Tech Square",
      "city": "Bengaluru",
      "state": "Karnataka",
      "zip_code": "560001",
      "country": "IN",
      "gstin": "29AAAAA1111A1Z1"
    }
  }
  ```

| Field | Type | Description | Validation |
|---|---|---|---|
| `phone_number` * | string | Contact number, saved to the registration and later the account. | Required. **7–20** characters, digits plus ` `, `+`, `(`, `)`, `-`. Must not belong to an active user or **another** pending registration. |
| `billing_address` * | object | Invoice address. Replaces any previously saved one. | Required. |
| `billing_address.line1` * | string | Street address. | Required. |
| `billing_address.city` * | string | City. | Required. |
| `billing_address.state` * | string | State or province. | Required. |
| `billing_address.zip_code` * | string | Postal code. | Required. |
| `billing_address.country` * | string | Country code. | Required. |
| `billing_address.gstin` | string | Indian GST identification number, for a tax invoice. | Optional, may be blank. Exactly **15** characters matching `^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]$`, with a state prefix in 01–38, 97, 98, or 99. Upper-cased before validation. |

- **Success Response (200 OK)** — paid plan:
  ```json
  {
    "status": true,
    "message": "Order created successfully",
    "data": {
      "razorpay_order_id": "order_MtX9…",
      "amount": 472000,
      "currency": "INR",
      "razorpay_key_id": "rzp_test_…"
    },
    "errors": null
  }
  ```

- **Success Response (200 OK)** — **free plan**, onboarding completes here:
  ```json
  {
    "status": true,
    "message": "Free onboarding completed successfully!",
    "data": {
      "is_free": true,
      "access": "eyJhbGciOiJIUzI1NiIs…",
      "refresh": "eyJhbGciOiJIUzI1NiIs…",
      "user": { "id": "5fa8…", "email": "jane@example.com", "first_name": "Jane", "last_name": "Doe" }
    },
    "errors": null
  }
  ```

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Order created — or, for a zero-total plan, the whole signup completed. **Check `data.is_free`.** | `Order created successfully` / `Free onboarding completed successfully!` | `null` |
| **400** | No plan on the registration. | `No plan selected` | `["PLAN_SELECTION_REQUIRED"]` |
| **400** | Phone belongs to an active user or another pending signup. | `Validation failed` | `{"phone_number": [...]}` |
| **400** | Phone fails the format rule. | `Validation failed` | `{"phone_number": [...]}` |
| **400** | GSTIN is the wrong length, format, or state prefix. | `Validation failed` | `{"billing_address": {"gstin": [...]}}` |
| **400** | A required billing field is missing. | `Validation failed` | `{"billing_address": {...}}` |
| **401** / **403** | Missing, expired, or non-temp token. | see [The temporary token](#the-temporary-token) | — |

> **Branch on `data.is_free`.** A free plan returns real JWTs and a `user` object — the flow is
> over, no `/verify/` call is needed. A paid plan returns gateway details and requires
> [§11](#11-verify-the-payment).

> **`amount` is in the minor unit (paisa)**, so `472000` means ₹4,720.00. It is an integer, unlike
> the string prices in `billing_breakdown`.

> **Safe to call more than once.** Re-submitting replaces the stored billing address and creates a
> new order, which is how "edit my address" and "retry after a failed payment" work. Cancel the
> previous order ([§12](#12-cancel-an-order)) if you do not want it left pending.

> **Only one billing address is kept per registration.** Each call hard-deletes the previous one.

---

### 11. Verify the Payment

The final step for a paid plan. Verifies the gateway signature, then creates the real account,
tenant, and subscription.

- **Path**: `POST /api/v1/onboard/verify/`
- **Auth**: temp token.

| Field | Type | Description | Validation |
|---|---|---|---|
| `razorpay_order_id` * | string | The order id from step 10. | Required. Must exist and belong to this registration. |
| `razorpay_payment_id` * | string | The payment id returned by the gateway checkout. | Required. |
| `razorpay_signature` * | string | The gateway's HMAC signature over the order and payment ids. | Required. Verified against the secret — **except** in test environments and when running with the dummy key, where verification is skipped. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "Payment verified. Welcome onboard!",
    "data": {
      "access": "eyJhbGciOiJIUzI1NiIs…",
      "refresh": "eyJhbGciOiJIUzI1NiIs…",
      "user": { "id": "5fa8…", "email": "jane@example.com", "first_name": "Jane", "last_name": "Doe" }
    },
    "errors": null
  }
  ```

**What succeeds atomically, under a row lock on the transaction:**

1. A `CustomUser` is created, carrying the password hash chosen at step 3 — so the user's original
   password works immediately.
2. A `Tenant` is created from `business_name`, with a subdomain derived from it (non-alphanumerics
   stripped, spaces to hyphens, a numeric suffix added on collision).
3. A `TenantUserMapping` links the two.
4. An **active** `Subscription` is created for the chosen plan.
5. The billing address and transaction are re-pointed at the real user.
6. The `TempUser` is marked onboarded and soft-deleted.
7. JWTs are issued and a [session](../user/sessions.md) is opened.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Payment verified and onboarding complete. | `Payment verified. Welcome onboard!` | `null` |
| **200** | **Already completed** — the webhook or an earlier call got there first. Fresh tokens are issued for the existing account. | `Payment verified. Welcome onboard!` | `null` |
| **400** | Signature does not verify. | `Validation failed` / signature error | `[...]` |
| **404** | No transaction for that order id, or it belongs to someone else. | `Transaction not found for this user` | `["TRANSACTION_NOT_FOUND"]` |
| **401** / **403** | Missing, expired, or non-temp token. | see [The temporary token](#the-temporary-token) | — |

> **Idempotent by design.** If the [webhook](./webhooks.md) completed the order first, this
> endpoint recognises the `SUCCESSFUL` transaction and issues tokens for the account that already
> exists rather than failing or duplicating it. Retrying after a network timeout is safe.

> **A "not yours" transaction is reported as 404, not 403** — the same message as a nonexistent
> one, so the response cannot be used to probe for other people's order ids.

> **Signature verification is skipped in test mode and with the dummy key.** Make sure
> `RAZORPAY_KEY_ID` is a real key in any environment that handles real money.

---

### 12. Cancel an Order

Abandon a pending order so it stops blocking the flow.

- **Path**: `POST /api/v1/onboard/cancel-order/`
- **Auth**: temp token.

| Field | Type | Description | Validation |
|---|---|---|---|
| `razorpay_order_id` * | string | The order to cancel. | Required. |

| Status | When | `message` |
|---|---|---|
| **200** | Cancelled — **or** no such order existed. Identical response either way. | `Order cancelled and deactivated successfully` |
| **401** / **403** | Missing, expired, or non-temp token. | see [The temporary token](#the-temporary-token) |

> **Always `200`, even for an unknown or already-cancelled order.** The endpoint deliberately does
> not distinguish, so it is safe to call defensively. It also means you cannot use it to check
> whether an order exists — use [resume-session](#7-resume-a-session) for that.

> **Cancelling does not refund.** It marks the local record cancelled and soft-deletes it; it does
> not tell the gateway anything. Only use it for orders that were never paid.

---

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **Abandoned `TempUser` records are never cleaned up.** A pending registration reserves its email and phone number indefinitely. | A user who abandons signup cannot start again with the same email — `register-pending` returns "A registration is already pending". They must use [resume-session](#7-resume-a-session) or [change-email](#6-change-the-email), neither of which is discoverable from the error. |
| 2 | **Names must be letters only.** `O'Brien`, `Mary Jane`, and any hyphenated name are rejected at step 3. | Real users cannot sign up. |
| 3 | **`check-phone` matches raw strings**, while nothing normalises phone numbers on this path. | The check can report "available" for a number already registered in another format; the conflict then surfaces at [§10](#10-create-the-order). |
| 4 | **`referral_code` is never validated.** Any string is stored on the tenant as `referred_by_code`. | Typos and invented codes are accepted silently. |
| 5 | **`update-plan` does not invalidate an existing order.** | A user who switches plans after creating an order can pay the old amount. |
| 6 | **The signup OTP brute-force counter is not cleared by resend or change-email.** | After five wrong attempts the user is locked out for 10 minutes even with a freshly emailed code. |

## Related

- [Plans API](./plans.md) — the `plan_id` and its pricing
- [Password reset for pending signups](./password_reset.md)
- [Razorpay webhook](./webhooks.md) — the gateway's own callback
- [Authentication API](../user/auth.md) — where users go once onboarded
- [Timezones](./timezones.md) · [Languages](./languages.md)
