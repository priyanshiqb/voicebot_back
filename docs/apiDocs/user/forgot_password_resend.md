# Resend Password-Reset OTP API Documentation

Re-sends the 6-digit password-reset code to an **existing, onboarded** user who has already started
a reset. Step 1 of the [password-reset flow](./auth.md#password-reset), repeated.

> **This is the onboarded-user endpoint.** Users who have not finished signing up have a separate
> one — see [onboard/forgot_password_resend](../onboard/forgot_password_resend.md). Sending an
> unfinished signup's email here gets a `200` and no email, because no `CustomUser` exists yet.

- **Path**: `POST /api/v1/auth/forgot-password/resend/`
- **Auth**: none. Any `Authorization` header is ignored.
- **Cooldown**: 30 seconds per account.

## Request

```json
{ "email": "user@example.com" }
```

`*` marks required.

| Field | Type | Description | Validation |
|---|---|---|---|
| `email` * | string | The account to resend the code to. | Required, must be a valid email address. Trimmed and lower-cased before lookup. |

## Responses

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | A new OTP was generated and queued — **or** the email is not registered, is inactive, or is soft-deleted. Identical response either way. | `A new verification OTP has been emailed.` | `null` |
| **400** | Less than 30 seconds since the last OTP for this account. | `Please wait before resending OTP.` | `["OTP_COOLDOWN_ACTIVE"]` |
| **400** | `email` missing, empty, or not a valid address. | `This field is required.` / `Enter a valid email address.` | `{"email": [...]}` |

**200 OK**
```json
{
  "status": true,
  "message": "A new verification OTP has been emailed.",
  "data": null,
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

> **`errors` is an array of string codes here, not the usual field map.** Branch on
> `errors[0] === "OTP_COOLDOWN_ACTIVE"` and use `data.cooldown_remaining` (whole seconds) to drive a
> countdown on the resend button.

> **Resending invalidates the previous code.** The stored OTP is overwritten, so only the newest
> email works. A user who clicks resend and then types the code from the older email gets
> `INVALID_OTP` at [verify](./auth.md#6-verify-otp-step-2).

> **The new code restarts the validity window** — `OTP_VALIDITY_MINUTES`, default **2 minutes** —
> and also restarts the 30-second cooldown.

> **Email failures are swallowed.** The OTP is stored and `200` returned even if queuing the email
> fails, so a user who receives nothing is still inside the cooldown and cannot immediately retry.

> **The success response hides whether the email exists — but the cooldown does not.** An
> unregistered address always returns `200`; a registered one inside the window returns `400`. Two
> rapid requests therefore distinguish the two. Recorded in
> [auth Known gaps](./auth.md#known-gaps).

## Related

- [Authentication API](./auth.md#password-reset) — the full four-step reset flow
- [onboard/forgot_password_resend](../onboard/forgot_password_resend.md) — the equivalent for unfinished signups
