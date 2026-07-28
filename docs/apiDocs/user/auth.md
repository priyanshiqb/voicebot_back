# User Authentication & Password Reset APIs

This document covers endpoints related to user login, logout, JWT token refresh, and the step-by-step forgot password/reset password flow for active onboarded users.

---

## 🔑 1. User Login

* **Path**: `/api/v1/auth/login/`
* **Method**: `POST`
* **Authentication**: None (Anonymous/AllowAny)
* **Description**: Authenticates active users via email and password, returning JWT access and refresh tokens.

### Request Payload

```json
{
  "email": "user@example.com",
  "password": "SecurePassword123"
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `email` | String | Yes | Must be a valid email format. | User's registered email address. |
| `password` | String | Yes | None (no length or complexity check on login). | User's plaintext password. |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Login successful.",
  "data": {
    "access": "eyJhbGciOi...",
    "refresh": "eyJhbGciOi...",
    "user": {
      "id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a",
      "email": "user@example.com",
      "first_name": "John",
      "last_name": "Doe"
    }
  },
  "errors": null
}
```

#### 400 Bad Request (Validation Error / Onboarding Pending Redirect)

##### Case A: Standard Validation/Credentials Error
```json
{
  "status": false,
  "message": "Invalid email or password format.",
  "data": null,
  "errors": {
    "email": ["Enter a valid email address."]
  }
}
```

##### Case B: Onboarding Pending Redirect
If the supplied email and password match a pending onboarding (`TempUser`) session, the API returns a `400 Bad Request` with an `onboarding_pending` flag and the session ID. The frontend can use this flag to redirect the user back to the signup/onboarding workflow:
```json
{
  "status": false,
  "message": "Your registration is still pending. Please complete your onboarding.",
  "data": null,
  "errors": {
    "onboarding_pending": true,
    "temp_registration_id": "b1a4cfd2-6d12-4cf4-9124-fa9a1d29bcfa",
    "email": "pending.user@example.com"
  }
}
```

##### Case C: Invalid Credentials
Wrong email/password (or the account does not exist). Returned as a `400` with a `credentials` errors key:
```json
{
  "status": false,
  "message": "Invalid email or password",
  "data": null,
  "errors": {
    "credentials": ["Invalid email or password"]
  }
}
```

##### Case D: Inactive Account
Credentials are valid but the user account is disabled. Returned as a `400` with an `account` errors key:
```json
{
  "status": false,
  "message": "User account is disabled",
  "data": null,
  "errors": {
    "account": ["User account is disabled"]
  }
}
```

#### 403 Forbidden (Account/IP Locked)

The only `403` this endpoint returns. Produced by django-defender after too many failed login attempts; the account or source IP is temporarily locked.
```json
{
  "status": false,
  "message": "Account or IP temporarily locked due to too many failed login attempts. Please try again in 1 hour.",
  "data": null,
  "errors": null
}
```

---

## 🚪 2. User Logout

* **Path**: `/api/v1/auth/logout/`
* **Method**: `POST`
* **Authentication**: Bearer JWT (IsAuthenticated)
* **Description**: Blacklists the provided refresh token to terminate the user session.

### Request Payload

```json
{
  "refresh": "eyJhbGciOi..."
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `refresh` | String | Yes | Must be a valid active refresh token. | The JWT refresh token to blacklist. |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Logout successful",
  "data": null,
  "errors": null
}
```

#### 400 Bad Request (Token Error)

Returned as a plain message with no `errors` array. The message is `"Refresh token is required."` when the field is missing, or `"Invalid refresh token."` when it is invalid/expired.
```json
{
  "status": false,
  "message": "Invalid refresh token.",
  "data": null,
  "errors": null
}
```

---

## 🔄 3. Token Refresh

* **Path**: `/api/v1/auth/refresh/`
* **Method**: `POST`
* **Authentication**: None
* **Description**: Issues a new JWT access token using an active refresh token. 
  * **Note on Inactivity Timeouts**: The new refresh token will automatically calculate its new absolute expiration date by reading the user's `session_timeout_hours` configuration. By rotating tokens regularly while the user is active, they will stay logged in; if they exceed `session_timeout_hours` without a refresh, the token expires and they are logged out.

### Request Payload

```json
{
  "refresh": "eyJhbGciOi..."
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `refresh` | String | Yes | Must be a valid active refresh token. | The JWT refresh token to verify. |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Token refreshed successfully.",
  "data": {
    "access": "eyJhbGciOi...",
    "refresh": "eyJhbGciOi..."
  },
  "errors": null
}
```

> **Note**: Refresh tokens are rotated (`ROTATE_REFRESH_TOKENS=True`), so a **new** `refresh` token is returned alongside the new `access` token. Clients must replace their stored refresh token with this rotated value.

#### 400 Bad Request (Token/Account Error)

Returned as a plain message with no `errors` array. Possible messages:
- `"Refresh token is required."`
- `"Invalid refresh token."`
- `"User account is inactive."`
- `"User account is deleted."`
- `"Session is invalid or has been logged out."`
- `"User not found."`

```json
{
  "status": false,
  "message": "Invalid refresh token.",
  "data": null,
  "errors": null
}
```

---

## 🔒 4. Password Reset - Step 1: Forgot Password

* **Path**: `/api/v1/auth/forgot-password/`
* **Method**: `POST`
* **Authentication**: None (Anonymous/AllowAny)
* **Description**: Generates a 6-digit OTP verification code valid for 2 minutes and emails it to the user.

### Request Payload

```json
{
  "email": "user@example.com"
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `email` | String | Yes | Must be a valid email format. | User's registered email address. |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "If the email is registered, a verification OTP code will be sent shortly.",
  "data": null,
  "errors": null
}
```

#### 400 Bad Request (Cooldown Active)

Returned if another OTP is requested within the 30-second cooldown window.
```json
{
  "status": false,
  "message": "Please wait before requesting another OTP.",
  "data": {
    "cooldown_remaining": 18
  },
  "errors": ["OTP_COOLDOWN_ACTIVE"]
}
```

---

## 🔁 5. Password Reset - Step 1 Helper: Resend Forgot Password OTP

* **Path**: `/api/v1/auth/forgot-password/resend/`
* **Method**: `POST`
* **Authentication**: None (Anonymous/AllowAny)
* **Description**: Resends a new 6-digit verification OTP to the user's email, subject to a 30-second cooldown period.

### Request Payload

```json
{
  "email": "user@example.com"
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `email` | String | Yes | Must be a valid email format. | User's registered email address. |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "A new verification OTP has been emailed.",
  "data": null,
  "errors": null
}
```

#### 400 Bad Request (Cooldown Active)

```json
{
  "status": false,
  "message": "Please wait before resending OTP.",
  "data": {
    "cooldown_remaining": 18
  },
  "errors": ["OTP_COOLDOWN_ACTIVE"]
}
```

---

## 🔍 6. Password Reset - Step 2: Verify Forgot Password OTP

* **Path**: `/api/v1/auth/forgot-password/verify/`
* **Method**: `POST`
* **Authentication**: None (Anonymous/AllowAny)
* **Description**: Verifies the received OTP. Upon success, returns a secure temporary signed reset token.

### Request Payload

```json
{
  "email": "user@example.com",
  "otp_code": "123456"
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `email` | String | Yes | Must be a valid email format. | User's registered email address. |
| `otp_code` | String | Yes | 5 to 6 characters (`min_length=5`, `max_length=6`). No numeric-only validation is enforced. | The OTP code sent to the email. |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "OTP verified successfully.",
  "data": {
    "token": "eyJhbGciOi..."
  },
  "errors": null
}
```

#### 400 Bad Request (Invalid/Expired OTP)

```json
{
  "status": false,
  "message": "Verification code has expired.",
  "data": null,
  "errors": ["OTP_EXPIRED"]
}
```

---

## 🔒 7. Password Reset - Step 3: Reset Password

* **Path**: `/api/v1/auth/reset-password/`
* **Method**: `POST`
* **Authentication**: None (Anonymous/AllowAny)
* **Description**: Uses the temporary signed token to perform the password update. The token is single-use.

### Request Payload

```json
{
  "email": "user@example.com",
  "token": "eyJhbGciOi...",
  "password": "NewSecurePassword123",
  "confirm_password": "NewSecurePassword123"
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `email` | String | Yes | Must be a valid email format. | User's registered email address. |
| `token` | String | Yes | Must be a valid, unexpired signed token. | The verification reset token. |
| `password` | String | Yes | 8 to 32 characters, no spaces; must contain at least one uppercase letter, one lowercase letter, one digit, and one special character. | New plaintext password. |
| `confirm_password` | String | Yes | Must match `password`. | Password confirmation. |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Password reset successfully.",
  "data": null,
  "errors": null
}
```

#### 400 Bad Request (Invalid/Expired Token or Validation Failure)

```json
{
  "status": false,
  "message": "This reset link has already been used.",
  "data": null,
  "errors": ["TOKEN_ALREADY_USED"]
}
```

---

## 🤖 8. Voicebot Agent Authentication

* **Description**: Programmatic API access for voicebot agents is secured using long-lived static tokens managed via the Django Admin interface.
* **Authentication Scheme**: Bearer Token
* **Header Format**: `Authorization: Bearer <agent_token_key>`

### Usage Requirements
- The user account associated with the token must have `user_type` set to `VB_AGENT`.
- The user account must be active.
- The `AgentToken` record must be active (`is_active=True`) and not expired (`expires_at` > now, if set).

### Example Request
```bash
curl -X GET "https://api.example.com/api/v1/some-agent-endpoint/" \
  -H "Authorization: Bearer <agent_token_key>"
```

### Responses

All agent authentication failures return **HTTP 401 Unauthorized** (raised as `AuthenticationFailed`).

#### 401 Unauthorized (Invalid Token)
If no matching active token prefix is found (invalid or inactive token):
```json
{
  "detail": "Invalid or inactive agent token."
}
```

#### 401 Unauthorized (Invalid Secret)
If the token prefix matches but the secret is wrong:
```json
{
  "detail": "Invalid agent token secret."
}
```

#### 401 Unauthorized (Expired Token)
```json
{
  "detail": "Agent token has expired."
}
```

#### 401 Unauthorized (Invalid User)
If the user account is inactive or not of type `VB_AGENT`:
```json
{
  "detail": "User is inactive or not an agent."
}
```
