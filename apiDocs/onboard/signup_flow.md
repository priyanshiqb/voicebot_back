# Onboarding Signup Flow APIs

This document covers endpoints involved in registering new temporary accounts, verifying emails with OTP, resuming sessions, changing emails, updating plans, creating orders, paying, verifying signatures, and cancelling orders.

---

## 📧 1. Check Email Availability

* **Path**: `/api/v1/onboard/check-email/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Asynchronously checks if an email address is available (not already in use by a verified or active user, nor a pending signup).

### Request Payload

```json
{
  "email": "test@example.com"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Email is available.",
  "data": {
    "available": true
  },
  "errors": null
}
```

#### 400 Bad Request

```json
{
  "status": false,
  "message": "Email is already registered.",
  "data": null,
  "errors": ["EMAIL_ALREADY_EXISTS"]
}
```

---

## ➕ 2. Step 1: User Signup Registration

* **Path**: `/api/v1/onboard/register-pending/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Registers a temporary unverified user, generates a 5-digit verification OTP valid for 2 minutes, and emails it to the user.

### Request Payload

```json
{
  "name": "Jane Doe",
  "email": "jane@example.com",
  "business_name": "Jane Corp",
  "password": "SecurePassword123",
  "referral_code": "optional-ref-code"
}
```

### Responses

#### 201 Created (Success)

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
  "message": "Registration failed.",
  "data": null,
  "errors": {
    "email": ["Email is already in use."]
  }
}
```

---

## 🔍 3. Step 2: Email OTP Verification

* **Path**: `/api/v1/onboard/verify-otp/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Verifies the 5-digit OTP sent to the email. Returns a temporary scoped onboarding JWT token and initial plan pricing.

### Request Payload

```json
{
  "temp_registration_id": "b1a4cfd2-6d12-4cf4-9124-fa9a1d29bcfa",
  "otp_code": "12345"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Email verified successfully.",
  "data": {
    "token": "eyJhbGciOi...",
    "plans": [
      {
        "id": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0",
        "name": "Starter",
        "price": 49.00
      }
    ]
  },
  "errors": null
}
```

#### 400 Bad Request (Invalid/Expired OTP)

```json
{
  "status": false,
  "message": "Invalid OTP code.",
  "data": null,
  "errors": ["INVALID_OTP"]
}
```

---

## 🔁 4. Step 2 Helper: Resend Verification OTP

* **Path**: `/api/v1/onboard/resend-otp/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Resends a new 5-digit verification OTP code to the temporary user's email, subject to a 30-second cooldown.

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

#### 400 Bad Request (Cooldown Active)

```json
{
  "status": false,
  "message": "Please wait before resending OTP.",
  "data": {
    "cooldown_remaining": 22
  },
  "errors": ["OTP_COOLDOWN_ACTIVE"]
}
```

---

## 🔄 5. Step 2 Helper: Change Temp User Email

* **Path**: `/api/v1/onboard/change-email/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Updates the email address of a temporary registration, generates a new 5-digit OTP, and sends it to the new address.

### Request Payload

```json
{
  "temp_registration_id": "b1a4cfd2-6d12-4cf4-9124-fa9a1d29bcfa",
  "new_email": "new.email@example.com"
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
  "message": "Email is already registered.",
  "data": null,
  "errors": ["EMAIL_ALREADY_EXISTS"]
}
```

---

## 🔌 6. Step 2 Helper: Resume Onboarding Session

* **Path**: `/api/v1/onboard/resume-session/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Resume onboarding process from the last saved state by providing email or registration ID.

### Request Payload

```json
{
  "email": "jane@example.com"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Session resumed successfully.",
  "data": {
    "temp_registration_id": "b1a4cfd2-6d12-4cf4-9124-fa9a1d29bcfa",
    "step": "VERIFIED_OTP",
    "email": "jane@example.com"
  },
  "errors": null
}
```

#### 400 Bad Request

```json
{
  "status": false,
  "message": "No pending registration found.",
  "data": null,
  "errors": ["SESSION_NOT_FOUND"]
}
```

---

## 📊 7. Step 3: Update Selected Plan

* **Path**: `/api/v1/onboard/update-plan/`
* **Method**: `PATCH`
* **Authentication**: Bearer Onboarding Scoped JWT
* **Description**: Updates the selected pricing plan for the temporary user.

### Request Payload

```json
{
  "plan_id": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Plan updated successfully.",
  "data": {
    "selected_plan": "Starter",
    "subtotal": 49.00,
    "tax": 8.82,
    "total": 57.82
  },
  "errors": null
}
```

#### 401 Unauthorized

```json
{
  "status": false,
  "message": "Authentication credentials were not provided.",
  "data": null,
  "errors": ["AUTHENTICATION_FAILED"]
}
```

---

## 💳 8. Step 4: Create Razorpay Order Just-In-Time

* **Path**: `/api/v1/onboard/create-order-just-in-time/`
* **Method**: `POST`
* **Authentication**: Bearer Onboarding Scoped JWT
* **Description**: Submits the billing address, optional GSTIN, and issues a Razorpay order.

### Request Payload

```json
{
  "billing_address": {
    "street": "123 Main St",
    "city": "Mumbai",
    "state": "Maharashtra",
    "postal_code": "400001",
    "country": "IN"
  },
  "gstin": "27AAACG0000A1Z5"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Order created successfully.",
  "data": {
    "order_id": "order_Hj2k9L20a1Jz",
    "amount": 5782,
    "currency": "INR",
    "key_id": "rzp_test_..."
  },
  "errors": null
}
```

#### 400 Bad Request

```json
{
  "status": false,
  "message": "GSTIN validation failed.",
  "data": null,
  "errors": ["INVALID_GSTIN"]
}
```

---

## ❌ 9. Step 4 Helper: Cancel Order / Discard Progress

* **Path**: `/api/v1/onboard/cancel-order/`
* **Method**: `POST`
* **Authentication**: Bearer Onboarding Scoped JWT
* **Description**: Cancels the current checkout progress and removes the temporary registration record.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Registration progress cleared successfully.",
  "data": null,
  "errors": null
}
```

---

## 🛡️ 10. Step 5: Verify Payment & Complete Onboarding

* **Path**: `/api/v1/onboard/verify/`
* **Method**: `POST`
* **Authentication**: Bearer Onboarding Scoped JWT
* **Description**: Cryptographically verifies the Razorpay payment signature. Upon success, creates the Tenant, maps the user, deletes the TempUser, and issues standard JWT tokens.

### Request Payload

```json
{
  "razorpay_payment_id": "pay_Hj3d9K21b8Lm",
  "razorpay_order_id": "order_Hj2k9L20a1Jz",
  "razorpay_signature": "e3b0c442..."
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Payment verified and registration completed successfully.",
  "data": {
    "access": "eyJhbGciOi...",
    "refresh": "eyJhbGciOi...",
    "user": {
      "id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a",
      "email": "jane@example.com"
    }
  },
  "errors": null
}
```

#### 400 Bad Request (Signature Mismatch)

```json
{
  "status": false,
  "message": "Payment signature verification failed.",
  "data": null,
  "errors": ["SIGNATURE_VERIFICATION_FAILED"]
}
```
