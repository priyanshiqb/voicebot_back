# Resend Onboarding Password-Reset OTP API Documentation

Re-sends the 6-digit password-reset code to an **unfinished signup**. Step 2 of the
[onboarding password reset](./password_reset.md), repeated.

> **This is the pending-signup endpoint.** Users with a real account use
> [`/api/v1/auth/forgot-password/resend/`](../user/forgot_password_resend.md). The two look alike
> but read different tables and identify the user differently — this one by
> `temp_registration_id`, the other by `email`.

- **Path**: `POST /api/v1/onboard/forgot-password/resend/`
- **Auth**: none. Any `Authorization` header is ignored.
- **Cooldown**: 30 seconds per registration.

## Request

```json
{ "temp_registration_id": "9f1c2b3a-…" }
```

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `temp_registration_id` * | UUID | The pending registration, returned by [step 1](./password_reset.md#1-request-a-reset-otp) or [register-pending](./signup_flow.md#3-start-the-signup). | Required, must be a valid UUID and match a pending registration. |

> **There is no `email` field.** If you only have the address, call
> [step 1](./password_reset.md#1-request-a-reset-otp) instead — it accepts an email and returns the
> id you need here.

## Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | A new OTP was generated and queued for email. | `A new verification OTP has been emailed.` | `null` |
| **400** | No pending registration with that id, or the id is not a UUID. | `Invalid registration ID` | `["REGISTRATION_NOT_FOUND"]` |
| **400** | Less than 30 seconds since the last OTP for this registration. | `Please wait before resending OTP.` | `["OTP_COOLDOWN_ACTIVE"]` |
| **400** | `temp_registration_id` missing or malformed. | `Validation failed` | `{"temp_registration_id": [...]}` |

**200 OK**
```json
{
  "status": true,
  "message": "A new verification OTP has been emailed.",
  "data": {
    "temp_registration_id": "9f1c2b3a-…",
    "email": "jane@example.com"
  },
  "errors": null
}
```

**400 Bad Request — cooldown active**
```json
{
  "status": false,
  "message": "Please wait before resending OTP.",
  "data": { "cooldown_remaining": 24 },
  "errors": ["OTP_COOLDOWN_ACTIVE"]
}
```

## Behaviour worth knowing

> **`errors` is an array of string codes, not the usual field map** — except for the serializer
> failure, which *is* a field map. Check the shape before indexing: `errors[0]` for the flow
> errors, `errors.temp_registration_id` for a malformed request.

> **The response echoes the registration's email back to you.** Useful for showing "we sent a code
> to j…@example.com" without storing it client-side.

> **Resending invalidates the previous code.** Only the newest email works; an older code returns
> `INVALID_OTP` at [verify](./password_reset.md#3-verify-the-otp).

> **The new code restarts the validity window** — `OTP_VALIDITY_MINUTES`, default **2 minutes** —
> and the 30-second cooldown.

> **It does not reset the brute-force counter.** After five wrong attempts the registration is
> locked for 10 minutes, and a fresh code will still return `TOO_MANY_ATTEMPTS` until that window
> passes.

> **Email failures are swallowed.** The OTP is stored and `200` returned even if queuing fails, so
> a user who receives nothing is still inside the cooldown.

> **No enumeration risk here.** Unlike step 1, this takes an opaque id rather than an email, so a
> `REGISTRATION_NOT_FOUND` reveals nothing about which addresses are registered.

## Related

- [Onboarding password reset](./password_reset.md) — the full four-step flow
- [Signup flow](./signup_flow.md) — where `temp_registration_id` comes from
- [user/forgot_password_resend](../user/forgot_password_resend.md) — the equivalent for real accounts
