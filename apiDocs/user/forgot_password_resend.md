# Resend Onboarded User Forgot Password OTP API

This endpoint resends a verification OTP code to an active onboarded user who requested a password reset.

## Endpoint Details

* **Path**: `/api/v1/user/forgot-password/resend/`
* **Method**: `POST`
* **Authentication**: None (Anonymous/AllowAny)
* **Rate Limit/Cooldown**: 30 seconds

## Request Payload

```json
{
  "user_id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a"
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `user_id` | UUID | Yes | Must be a valid UUID string. Must match an active, non-deleted user. | The unique identifier of the user requesting the reset. |

## Responses

### 200 OK (Success)

Sent when the OTP email delivery is successfully triggered.

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

### 400 Bad Request (Validation Error)

Sent if fields are malformed or invalid.

```json
{
  "status": false,
  "message": "Invalid user ID.",
  "data": null,
  "errors": [
    "USER_NOT_FOUND"
  ]
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
