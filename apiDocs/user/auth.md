# User Authentication & Password Reset APIs

This document covers endpoints related to user login, logout, JWT token refresh, and the step-by-step forgot password/reset password flow for active onboarded users.

---

## 🔑 1. User Login

* **Path**: `/api/v1/user/login/`
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
| `password` | String | Yes | Minimum 8 characters. | User's plaintext password. |

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
      "username": "user123",
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

#### 403 Forbidden (Authentication Failure)

```json
{
  "status": false,
  "message": "Invalid credentials or inactive account.",
  "data": null,
  "errors": ["INVALID_CREDENTIALS"]
}
```

---

## 🚪 2. User Logout

* **Path**: `/api/v1/user/logout/`
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
  "message": "Successfully logged out.",
  "data": null,
  "errors": null
}
```

#### 400 Bad Request (Token Error)

```json
{
  "status": false,
  "message": "Token is invalid or expired.",
  "data": null,
  "errors": ["INVALID_TOKEN"]
}
```

---

## 🔄 3. Token Refresh

* **Path**: `/api/v1/user/token-refresh/`
* **Method**: `POST`
* **Authentication**: None
* **Description**: Issues a new JWT access token using an active refresh token.

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
    "access": "eyJhbGciOi..."
  },
  "errors": null
}
```

#### 400 Bad Request (Token Error)

```json
{
  "status": false,
  "message": "Token is invalid or expired.",
  "data": null,
  "errors": ["INVALID_TOKEN"]
}
```

---

## 🔒 4. Password Reset - Step 1: Forgot Password

* **Path**: `/api/v1/user/forgot-password/`
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
| `email` | String | Yes | Must match an active registered user. | User's registered email address. |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "OTP generation flow initiated successfully.",
  "data": {
    "user_id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a",
    "email": "user@example.com"
  },
  "errors": null
}
```

#### 400 Bad Request (Validation Error)

```json
{
  "status": false,
  "message": "Invalid email address.",
  "data": null,
  "errors": ["USER_NOT_FOUND"]
}
```

---

## 🔁 5. Password Reset - Step 1 Helper: Resend Forgot Password OTP

* **Path**: `/api/v1/user/forgot-password/resend/`
* **Method**: `POST`
* **Authentication**: None (Anonymous/AllowAny)
* **Description**: Resends a new 6-digit verification OTP to the user's email, subject to a 30-second cooldown period.

### Request Payload

```json
{
  "user_id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a"
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `user_id` | UUID | Yes | Must match an active registered user. | User's unique identifier. |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "A new verification OTP has been emailed.",
  "data": {
    "user_id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a",
    "email": "user@example.com"
  },
  "errors": null
}
```

#### 400 Bad Request (Cooldown Active or User Error)

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

* **Path**: `/api/v1/user/forgot-password/verify/`
* **Method**: `POST`
* **Authentication**: None (Anonymous/AllowAny)
* **Description**: Verifies the received OTP. Upon success, returns a secure temporary signed reset token.

### Request Payload

```json
{
  "user_id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a",
  "otp_code": "123456"
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `user_id` | UUID | Yes | Must match an active registered user. | User's unique identifier. |
| `otp_code` | String | Yes | Must be a 6-digit numeric string. | The OTP code sent to the email. |

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

* **Path**: `/api/v1/user/reset-password/`
* **Method**: `POST`
* **Authentication**: None (Anonymous/AllowAny)
* **Description**: Uses the temporary signed token to perform the password update. The token is single-use.

### Request Payload

```json
{
  "user_id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a",
  "token": "eyJhbGciOi...",
  "password": "NewSecurePassword123"
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `user_id` | UUID | Yes | Must match an active registered user. | User's unique identifier. |
| `token` | String | Yes | Must be a valid, unexpired signed token. | The verification reset token. |
| `password` | String | Yes | Minimum 8 characters. | New plaintext password. |

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
