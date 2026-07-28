# Resend Onboarded User Forgot Password OTP API

This endpoint resends a verification OTP code to an active onboarded user who requested a password reset.

## Endpoint Details

* **Path**: `/api/v1/auth/forgot-password/resend/`
* **Method**: `POST`
* **Authentication**: None (Anonymous/AllowAny)
* **Rate Limit/Cooldown**: 30 seconds

## Request Payload

```json
{
  "email": "user@example.com"
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `email` | String | Yes | Must be a valid email format. | The registered email address of the user requesting the reset. |

## Responses

### 200 OK (Success)

Sent when the OTP email delivery is successfully triggered (or silently skipped if user doesn't exist, preventing enumeration).

```json
{
  "status": true,
  "message": "A new verification OTP has been emailed.",
  "data": null,
  "errors": null
}
```

### 400 Bad Request (Cooldown Active)

Sent when resending is attempted within the 30-second cooldown period.

```json
{
  "status": false,
  "message": "Please wait before resending OTP.",
  "data": {
    "cooldown_remaining": 24
  },
  "errors": [
    "OTP_COOLDOWN_ACTIVE"
  ]
}
```
