# Onboarding Password Reset API Documentation

Resets the password of an **unfinished signup** — someone who registered but has not completed
payment, and so has no real account yet.

**Base path:** `/api/v1/onboard/`

> **This is not the reset flow for real users.** An onboarded account uses
> [`/api/v1/auth/forgot-password/`](../user/auth.md#password-reset). The two are near-identical in
> shape but operate on different tables, and neither finds the other's records. If you are unsure
> which a user is, [`resume-session`](./signup_flow.md#7-resume-a-session) will tell you —
> it returns `USER_ALREADY_ONBOARDED` for a finished account.

Why this exists at all: a `TempUser` holds a password chosen at
[registration](./signup_flow.md#3-start-the-signup), and that password is needed to
[resume a session](./signup_flow.md#7-resume-a-session). Forgetting it would otherwise strand the
signup permanently, because the email is already reserved.

## The flow

```
POST /onboard/forgot-password/          → OTP emailed, returns temp_registration_id
POST /onboard/forgot-password/resend/   → new OTP, 30s cooldown
POST /onboard/forgot-password/verify/   → returns a signed reset token (30 min)
POST /onboard/reset-password/           → sets the new password
```

All four are **anonymous** — no token of any kind. The reset token from step 3 is the only
credential, and it is passed in the body rather than a header.

## Endpoints Summary

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/onboard/forgot-password/` | Email a reset OTP |
| `POST` | `/onboard/forgot-password/resend/` | Re-send it |
| `POST` | `/onboard/forgot-password/verify/` | Exchange the OTP for a reset token |
| `POST` | `/onboard/reset-password/` | Set the new password |

---

## Endpoint Details

### 1. Request a Reset OTP

- **Path**: `POST /api/v1/onboard/forgot-password/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `email` * | string | The email used at registration. | Required, valid email. Trimmed and lower-cased. |

- **Success Response (200 OK)** — registration found:
  ```json
  {
    "status": true,
    "message": "If the email is registered in our temporary signup system, a verification OTP code will be sent shortly.",
    "data": { "temp_registration_id": "9f1c…" },
    "errors": null
  }
  ```

- **Success Response (200 OK)** — no such registration: identical `message`, but **`data` is `null`**.

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | OTP generated and emailed. `data.temp_registration_id` is returned. | the neutral message above | `null` |
| **200** | No pending registration for that email. `data` is `null`. | the same neutral message | `null` |
| **400** | Under 30 seconds since the last OTP. `data.cooldown_remaining` holds the seconds left. | `Please wait before requesting another OTP.` | `["OTP_COOLDOWN_ACTIVE"]` |
| **400** | `email` missing or malformed. | `Validation failed` | `{"email": [...]}` |

> **You need `data.temp_registration_id` for every later step**, so a `data: null` response means
> the flow cannot continue — there is nothing pending for that address.

> **The wording is neutral but the payload is not.** The message is identical either way, yet the
> presence of `data` reveals whether a pending registration exists. Recorded in
> [Known gaps](#known-gaps).

---

### 2. Resend the OTP

- **Path**: `POST /api/v1/onboard/forgot-password/resend/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `temp_registration_id` * | UUID | From step 1. | Required. Must match a pending registration. |

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | New OTP emailed. `data` carries `temp_registration_id` and `email`. | `A new verification OTP has been emailed.` | `null` |
| **400** | Unknown or malformed id. | `Invalid registration ID` | `["REGISTRATION_NOT_FOUND"]` |
| **400** | Under 30 seconds since the last OTP. | `Please wait before resending OTP.` | `["OTP_COOLDOWN_ACTIVE"]` |
| **400** | `temp_registration_id` missing or not a UUID. | `Validation failed` | `{"temp_registration_id": [...]}` |

> **Unlike step 1, this identifies the registration by id, not email** — so it cannot be used to
> probe for addresses, and it returns the email back to you in `data`.

> **Resending overwrites the previous code** and restarts both the validity window and the
> cooldown.

---

### 3. Verify the OTP

- **Path**: `POST /api/v1/onboard/forgot-password/verify/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `temp_registration_id` * | UUID | From step 1. | Required. Must match a pending registration. |
| `otp_code` * | string | The 6-digit code from the email. | Required. **5–6 characters** — the field accepts 5, though issued codes are always 6. |

- **Success Response (200 OK)**:
  ```json
  {
    "status": true,
    "message": "OTP verified successfully",
    "data": { "token": "eyJ0ZW1wX3VzZXJfaWQiOi…:1abcDe:xY…" },
    "errors": null
  }
  ```

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Correct and unexpired. The OTP is cleared and a **30-minute** reset token returned. | `OTP verified successfully` | `null` |
| **400** | Unknown or malformed registration id. | `Invalid registration ID` | `["REGISTRATION_NOT_FOUND"]` |
| **400** | Wrong code. | `Invalid OTP code` | `["INVALID_OTP"]` |
| **400** | Correct code, but older than `OTP_VALIDITY_MINUTES` (default **2**). | `Verification code has expired` | `["OTP_EXPIRED"]` |
| **400** | Five wrong attempts within 10 minutes. **The OTP is destroyed** — go back to step 2. | `Too many failed attempts. Verification code has been invalidated.` | `["TOO_MANY_ATTEMPTS"]` |

> **The attempt counter is separate from the signup OTP's.** Failing this five times does not lock
> [signup verification](./signup_flow.md#4-verify-the-otp), and vice versa — they use different
> cache keys.

---

### 4. Set the New Password

- **Path**: `POST /api/v1/onboard/reset-password/`

| Field | Type | Description | Validation |
|---|---|---|---|
| `temp_registration_id` * | UUID | The registration being reset. | Required. Must match the token's owner. |
| `token` * | string | The signed token from step 3. | Required. Valid signature, under **30 minutes** old, and not already used. |
| `password` * | string | The new password. | Required. **8–32** characters, no spaces, at least one uppercase, one lowercase, one digit, and one of `!@#$%^&*(),.?":{}\|<>`. |
| `confirm_password` * | string | Repeat of `password`. | Required. Must match exactly. |

| Status | When | `message` | `errors` |
|---|---|---|---|
| **200** | Password updated. The signup can continue. | `Password reset successfully. You can now verify OTP or proceed with onboarding.` | `null` |
| **400** | Unknown or malformed registration id. | `Invalid registration ID` | `["REGISTRATION_NOT_FOUND"]` |
| **400** | The token is more than 30 minutes old. | `The reset link has expired. Please request a new one.` | `["TOKEN_EXPIRED"]` |
| **400** | The token was tampered with or is not ours. | `The reset link is invalid.` | `["INVALID_TOKEN"]` |
| **400** | The token belongs to a different registration. | `Token does not match this registration ID.` | `["INVALID_TOKEN_SCOPE"]` |
| **400** | The token was already used. | `This reset link has already been used.` | `["TOKEN_ALREADY_USED"]` |
| **400** | A password rule failed, or the two do not match. | `Validation failed` | `{"password": [...]}` / `{"confirm_password": "Passwords do not match."}` |

> **Single use is enforced by embedding the old password hash in the token.** Once the password
> changes the hash no longer matches, so the token is spent.

> **This does not verify the email or advance the signup.** It only changes the stored password.
> The user still resumes at whatever step they were on — call
> [`resume-session`](./signup_flow.md#7-resume-a-session) with the new password to find out which.

---

## Differences from the onboarded-user reset

Worth a glance if you are implementing both against one UI.

| | **This flow** (pending signup) | [**Auth flow**](../user/auth.md#password-reset) (real account) |
|---|---|---|
| Identifies by | `temp_registration_id` after step 1 | `email` throughout |
| Step 1 returns | `temp_registration_id` in `data` | `data: null` always |
| Resend identified by | `temp_registration_id` | `email` |
| Reset requires | `temp_registration_id` + `token` | `email` + `token` |
| `message` on validation failure | always `Validation failed` | the specific field error |
| Afterwards | resume the signup | log in |

> **The two flows are not interchangeable and give unhelpful errors when confused.** Sending a real
> user's email here returns the neutral `200` with `data: null` and no email is ever sent — the
> user waits for a code that will not arrive. Route on account state before choosing a flow.

## Known gaps

| # | Gap | Impact |
|---|---|---|
| 1 | **Step 1 leaks whether a registration exists** — not through `message`, which is neutral, but through the presence of `data.temp_registration_id`. | Address enumeration against the pending-signup table. |
| 2 | **The cooldown branch leaks the same thing more loudly**: an unknown email returns `200`, a known one inside the window returns `400`. | Same, in one extra request. |
| 3 | **Sending an onboarded user's email here silently does nothing.** | A user who picks the wrong "forgot password" link gets a success message and no email, with no way to tell why. |
| 4 | **OTP emails are best-effort.** A queuing failure still returns `200`, and the user remains inside the 30-second cooldown. | A user who receives nothing cannot immediately retry. |

## Related

- [Signup flow](./signup_flow.md) — where the password is first set, and where the user returns
- [Authentication API](../user/auth.md#password-reset) — the equivalent for real accounts
- [onboard/forgot_password_resend](./forgot_password_resend.md) — step 2 in detail
