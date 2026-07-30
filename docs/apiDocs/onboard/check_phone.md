# Onboarding - Check Phone API Documentation

## Endpoint
**POST** `/api/v1/onboard/check-phone/`

## Description
Checks whether a phone number already exists in either the active `CustomUser` or pending
`TempUser` tables. Both lookups are indexed existence checks, so the endpoint is cheap
enough to call as the user types during the onboarding signup flow.

Public and unauthenticated (`AllowAny`).

## Request Payload

### HTTP Headers
* None (Anonymous Public)

### Body (JSON)
```json
{
  "phone_number": "919876543210"
}
```

### Explicit Field Validations
* `phone_number` (String, Required): 
  * The phone number to check for uniqueness. Must be provided as a string.

## Response Types & Status Codes

### Success - Available/Not Available (HTTP 200 OK)
Returns HTTP 200 OK regardless of whether the phone number is available or not. The `available` boolean flag within the data payload determines uniqueness.

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
*Note: If `exists_in_active_users` or `exists_in_pending_registrations` is `true`, then `available` will be `false`.*

## Edge Cases & Error Handling

### Bad Request - Validation Failed (HTTP 400 Bad Request)
Returned when required fields are missing from the request payload.

```json
{
  "status": false,
  "message": "Validation failed",
  "data": null,
  "errors": {
    "phone_number": [
      "This field is required."
    ]
  }
}
```

## Search & Filtering Capabilities
* Not applicable for this endpoint (Accepts direct POST payload checks only).
