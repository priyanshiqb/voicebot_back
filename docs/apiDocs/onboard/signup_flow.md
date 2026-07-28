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

> Availability is always reported with `200 OK`. An already-registered email returns `200` with `available: false` (never a 400). The `message` reflects whether the check resolved via the Bloom-filter fast-path (`"Email availability checked (fast-path)."`) or the database slow-path (`"Email availability checked (slow-path)."`).

```json
{
  "status": true,
  "message": "Email availability checked (fast-path).",
  "data": {
    "email": "test@example.com",
    "available": true,
    "exists_in_active_users": false,
    "exists_in_pending_registrations": false
  },
  "errors": null
}
```

#### 400 Bad Request (Validation Error)

Returned only when the payload itself is malformed (e.g. an invalid email format).

```json
{
  "status": false,
  "message": "Validation failed",
  "data": null,
  "errors": {
    "email": ["Enter a valid email address."]
  }
}
```

---

## 📱 1.1. Check Phone Availability

* **Path**: `/api/v1/onboard/check-phone/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Checks if a phone number is already in use by an active user or a pending registration. Availability is always reported with `200 OK`.

### Request Payload

```json
{
  "phone_number": "919876543210"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Phone number availability checked.",
  "data": {
    "phone_number": "919876543210",
    "available": true,
    "exists_in_active_users": false,
    "exists_in_pending_registrations": false
  },
  "errors": null
}
```

#### 400 Bad Request (Validation Error)

```json
{
  "status": false,
  "message": "Validation failed",
  "data": null,
  "errors": {
    "phone_number": ["This field is required."]
  }
}
```

---

## ➕ 2. Step 1: User Signup Registration

* **Path**: `/api/v1/onboard/register-pending/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Registers a temporary unverified user, generates a 6-digit verification OTP valid for 2 minutes, and emails it to the user.

### Request Payload

| Field | Type | Required | Constraints |
| :--- | :--- | :--- | :--- |
| `first_name` | String | Yes | Alphabetic only, 2–30 characters. |
| `last_name` | String | Yes | Alphabetic only, 2–30 characters. |
| `email` | String | Yes | Valid email, 6–100 characters. |
| `business_name` | String | Yes | 2–255 characters. |
| `password` | String | Yes | 8–32 chars; at least one uppercase, lowercase, digit, and special character; no spaces. |
| `confirm_password` | String | Yes | Must match `password`. |
| `plan_id` | UUID | Yes | Must reference an active plan. |
| `timezone_id` | UUID | No | Must reference an existing timezone. |
| `language_id` | UUID | No | Must reference an existing language. |
| `referral_code` | String | No | Up to 50 characters. |

```json
{
  "first_name": "Jane",
  "last_name": "Doe",
  "email": "jane@example.com",
  "business_name": "Jane Corp",
  "password": "SecurePass@123",
  "confirm_password": "SecurePass@123",
  "plan_id": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0",
  "timezone_id": "e305e94b-9e4b-4b2a-88fa-10a905a96ab1",
  "language_id": "f516a24b-9e4b-4b2a-88fa-10a905a96db2",
  "referral_code": "optional-ref-code"
}
```

### Responses

#### 201 Created (Success)

```json
{
  "status": true,
  "message": "Registration initiated. Verification OTP has been emailed.",
  "data": {
    "temp_registration_id": "b1a4cfd2-6d12-4cf4-9124-fa9a1d29bcfa",
    "email": "jane@example.com"
  },
  "errors": null
}
```

#### 400 Bad Request

```json
{
  "status": false,
  "message": "Validation failed",
  "data": null,
  "errors": {
    "email": ["A registration is already pending for this email."]
  }
}
```

---

## 🔍 3. Step 2: Email OTP Verification

* **Path**: `/api/v1/onboard/verify-otp/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Verifies the 6-digit OTP sent to the email. Returns a temporary scoped onboarding JWT token and initial plan pricing.

### Request Payload

```json
{
  "temp_registration_id": "b1a4cfd2-6d12-4cf4-9124-fa9a1d29bcfa",
  "otp_code": "123456"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Email verified successfully",
  "data": {
    "temporary_token": "eyJhbGciOi...",
    "email": "jane@example.com",
    "plan": {
      "id": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0",
      "name": "Starter Plan",
      "code": "starter_inr"
    },
    "billing_breakdown": {
      "base_price": "2000.00",
      "discount_amount": "200.00",
      "net_base_price": "1800.00",
      "tax_percentage": "18.00",
      "tax_amount": "324.00",
      "total_price": "2124.00",
      "currency": "INR"
    }
  },
  "errors": null
}
```

#### 400 Bad Request (Invalid/Expired OTP)

```json
{
  "status": false,
  "message": "Invalid OTP code",
  "data": null,
  "errors": ["INVALID_OTP"]
}
```

---

## 🔁 4. Step 2 Helper: Resend Verification OTP

* **Path**: `/api/v1/onboard/resend-otp/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Resends a new 6-digit verification OTP code to the temporary user's email, subject to a 30-second cooldown.

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
* **Description**: Updates the email address of a temporary registration, generates a new 6-digit OTP, and sends it to the new address.

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
  "message": "A user with this email already exists.",
  "data": null,
  "errors": { "email": ["A user with this email already exists."] }
}
```
> If a registration is already **pending** for the email, the message is instead
> `"A registration is already pending for this email."` (same `errors.email` shape).

---

## 🔌 6. Step 2 Helper: Resume Onboarding Session

* **Path**: `/api/v1/onboard/resume-session/`
* **Method**: `POST`
* **Authentication**: None (Anonymous)
* **Description**: Resume onboarding process from the last saved state by providing email or registration ID, along with the password chosen during signup.

### Request Payload

```json
{
  "email": "jane@example.com",
  "password": "SecurePassword123"
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
  "message": "Incorrect password",
  "data": null,
  "errors": ["INVALID_CREDENTIALS"]
}
```

---

## 🔌 6.1. Step 2 Helper: Resume Onboarding Session with Token

* **Path**: `/api/v1/onboard/resume-session-token/`
* **Method**: `POST`
* **Authentication**: Bearer Temporary Token (passed via `Authorization` header)
* **Description**: Resume onboarding process from the last saved state by passing the temporary token in the `Authorization` header. No request body required.

### Request

```
POST /api/v1/onboard/resume-session-token/
Authorization: Bearer eyJhbGciOi...
```

> No request body needed. The token in the header cryptographically identifies the user.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Session resumed successfully.",
  "data": {
    "temp_registration_id": "b1a4cfd2-6d12-4cf4-9124-fa9a1d29bcfa",
    "email": "jane@example.com",
    "phone_number": "919876543210",
    "step": "BILLING_ADDRESS_REQUIRED",
    "temporary_token": "eyJhbGciOi...",
    "plan": {
      "id": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0",
      "name": "Starter Plan",
      "code": "starter_inr"
    },
    "billing_breakdown": {
      "base_price": "2000.00",
      "discount_amount": "200.00",
      "net_base_price": "1800.00",
      "tax_percentage": "18.00",
      "tax_amount": "324.00",
      "total_price": "2124.00",
      "currency": "INR"
    },
    "billing_address": null,
    "razorpay_order_id": null
  },
  "errors": null
}
```

#### 401 Unauthorized (Invalid / Expired Token)

```json
{
  "status": false,
  "message": "Invalid temporary token or user registration ID",
  "data": null,
  "errors": ["INVALID_TEMPORARY_TOKEN"]
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
  "message": "Plan updated successfully",
  "data": {
    "plan": {
      "id": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0",
      "name": "Starter Plan",
      "code": "starter_inr"
    },
    "billing_breakdown": {
      "base_price": "2000.00",
      "discount_amount": "200.00",
      "net_base_price": "1800.00",
      "tax_percentage": "18.00",
      "tax_amount": "324.00",
      "total_price": "2124.00",
      "currency": "INR"
    }
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
* **Description**: Submits the phone number and billing address (with optional GSTIN nested inside `billing_address`) and issues a Razorpay order. For a zero-cost (free) plan, Razorpay is skipped and onboarding is completed immediately, returning permanent JWT tokens.

### Request Payload

`phone_number` is required at the top level. `gstin` is optional and nested **inside** `billing_address`. Billing address fields are `line1`, `city`, `state`, `zip_code`, `country`, and `gstin`.

```json
{
  "phone_number": "919876543210",
  "billing_address": {
    "line1": "123 Main St",
    "city": "Mumbai",
    "state": "Maharashtra",
    "zip_code": "400001",
    "country": "IN",
    "gstin": "27AAACG0000A1Z5"
  }
}
```

### Responses

#### 200 OK (Success — Paid Plan)

`amount` is expressed in paisa (`total_price` × 100).

```json
{
  "status": true,
  "message": "Razorpay order initiated",
  "data": {
    "razorpay_order_id": "order_Hj2k9L20a1Jz",
    "amount": 212400,
    "currency": "INR",
    "razorpay_key_id": "rzp_test_..."
  },
  "errors": null
}
```

#### 200 OK (Success — Free Plan)

When the selected plan resolves to a zero total, Razorpay is bypassed, the account is created immediately, and permanent tokens are returned.

```json
{
  "status": true,
  "message": "Free onboarding completed successfully!",
  "data": {
    "is_free": true,
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

#### 400 Bad Request (Validation Error)

```json
{
  "status": false,
  "message": "Validation failed",
  "data": null,
  "errors": {
    "billing_address": {
      "gstin": ["Please enter a valid GSTIN format (e.g. 07AAAAA1111A1Z1)."]
    }
  }
}
```

---

## ❌ 9. Step 4 Helper: Cancel Order / Discard Progress

* **Path**: `/api/v1/onboard/cancel-order/`
* **Method**: `POST`
* **Authentication**: Bearer Onboarding Scoped JWT
* **Description**: Cancels the Razorpay order identified by `razorpay_order_id` for the temporary user, marking its transaction as canceled and soft-deleting it.

### Request Payload

```json
{
  "razorpay_order_id": "order_Hj2k9L20a1Jz"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Order cancelled and deactivated successfully",
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
