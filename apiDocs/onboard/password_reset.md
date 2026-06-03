# Onboarding Temporary User Password Reset APIs

This document covers endpoints related to resetting passwords of unverified temporary registrations before they complete onboarding.

---

## 🔑 1. Step 1: Forgot Temp User Password

* **Path**: `/api/v1/onboard/forgot-password/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Initiates password reset for unverified temporary users by emailing a 6-digit OTP verification code valid for 2 minutes.

### Request Payload

```json
{
  "email": "tempuser@example.com"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Verification code has been sent to your email.",
  "data": {
    "temp_registration_id": "b1a4cfd2-6d12-4cf4-9124-fa9a1d29bcfa"
  },
  "errors": null
}
```

#### 400 Bad Request

```json
{
  "status": false,
  "message": "Temporary user not found.",
  "data": null,
  "errors": ["USER_NOT_FOUND"]
}
```

---

## 🔁 2. Step 1 Helper: Resend Forgot Password OTP

* **Path**: `/api/v1/onboard/forgot-password/resend/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Resends a new 6-digit OTP to the temporary user's email, subject to a 30-second cooldown.

### Request Payload

```json
{
  "temp_registration_id": "b1a4cfd2-6d12-4cf4-9124-fa9a1d29bcfa"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Verification code has been sent to your email.",
  "data": {
    "temp_registration_id": "b1a4cfd2-6d12-4cf4-9124-fa9a1d29bcfa"
  },
  "errors": null
}
```

#### 400 Bad Request

```json
{
  "status": false,
  "message": "Please wait before resending OTP.",
  "data": {
    "cooldown_remaining": 15
  },
  "errors": ["OTP_COOLDOWN_ACTIVE"]
}
```

---

## 🔍 3. Step 2: Verify Forgot Password OTP

* **Path**: `/api/v1/onboard/forgot-password/verify/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Verifies the emailed 6-digit OTP. Upon successful verification, invalidates the OTP and returns a secure temporary signed reset token.

### Request Payload

```json
{
  "temp_registration_id": "b1a4cfd2-6d12-4cf4-9124-fa9a1d29bcfa",
  "otp_code": "654321"
}
```

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

#### 400 Bad Request

```json
{
  "status": false,
  "message": "Invalid OTP code.",
  "data": null,
  "errors": ["INVALID_OTP"]
}
```

---

## 🔒 4. Step 3: Reset Temp User Password

* **Path**: `/api/v1/onboard/reset-password/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Resets the password of the temporary onboarding registration using a secure one-time-use token.

### Request Payload

```json
{
  "temp_registration_id": "b1a4cfd2-6d12-4cf4-9124-fa9a1d29bcfa",
  "token": "eyJhbGciOi...",
  "password": "NewSecurePassword123"
}
```

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

#### 400 Bad Request

```json
{
  "status": false,
  "message": "Reset token has expired.",
  "data": null,
  "errors": ["TOKEN_EXPIRED"]
}
```
