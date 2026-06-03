# Resend Onboarding Forgot Password OTP API

This endpoint resends a verification OTP code to a temporary user in the onboarding signup system who requested a password reset.

## Endpoint Details

* **Path**: `/api/v1/onboard/forgot-password/resend/`
* **Method**: `POST`
* **Authentication**: None (Anonymous/AllowAny)
* **Rate Limit/Cooldown**: 30 seconds

## Request Payload

```json
{
  "temp_registration_id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a"
}
```

### Fields and Validation

| Field | Type | Required | Constraints / Validation | Description |
| :--- | :--- | :--- | :--- | :--- |
| `temp_registration_id` | UUID | Yes | Must be a valid UUID string. Must match an existing temporary onboarding registration. | The temporary registration ID returned during onboarding signup/reset step. |

## Responses

### 200 OK (Success)

Sent when the OTP email delivery is successfully triggered.

```json
{
  "status": true,
  "message": "A new verification OTP has been emailed.",
  "data": {
    "temp_registration_id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a",
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
  "message": "Invalid registration ID",
  "data": null,
  "errors": [
    "REGISTRATION_NOT_FOUND"
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
