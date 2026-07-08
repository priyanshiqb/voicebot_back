# Users Management API

This document covers endpoints related to user listing, details, creation, update, deletion, and retrieval of the currently authenticated user session.

---

## 🔍 1. List Users

* **Path**: `/api/v1/user/users/`
* **Method**: `GET`
* **Authentication**: Bearer JWT (IsAuthenticated)
* **Description**: Returns a paginated list of users.

### Query Parameters

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `is_active` | Boolean | No | Filter by active state. |
| `search` | String | No | Search text matching email, username, first_name, last_name, or phone_number. |
| `ordering` | String | No | Order by `created_at`, `updated_at`, `email`, `first_name`, `last_name`. Prefix with `-` for descending. |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "List retrieved successfully.",
  "data": {
    "count": 1,
    "next": null,
    "previous": null,
    "results": [
      {
        "id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a",
        "email": "user@example.com",
        "first_name": "John",
        "last_name": "Doe",
        "phone_number": "+1234567890",
        "is_active": true,
        "is_verified": true,
        "session_timeout_hours": 168,
        "created_at": "2026-06-03T10:00:00Z",
        "timezone": {
          "id": "a9a3b8d4-53c2-4091-9876-fa8b0c29f4a1",
          "timezone": "Asia/Kolkata",
          "offset": "UTC+05:30"
        },
        "language": {
          "id": "f516a24b-9e4b-4b2a-88fa-10a905a96db2",
          "name": "English (India)",
          "code": "en-IN"
        }
      }
    ]
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

## 👤 2. Get Current User ("Me")

* **Path**: `/api/v1/user/users/me/`
* **Method**: `GET`
* **Authentication**: Bearer JWT (IsAuthenticated)
* **Description**: Retrieve detailed profile of the currently logged-in user.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Details retrieved successfully.",
  "data": {
    "id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a",
    "email": "user@example.com",
    "first_name": "John",
    "last_name": "Doe",
    "phone_number": "+1234567890",
    "is_active": true,
    "is_verified": true,
    "session_timeout_hours": 168,
    "created_at": "2026-06-03T10:00:00Z",
    "updated_at": "2026-06-03T10:00:00Z",
    "timezone": {
      "id": "a9a3b8d4-53c2-4091-9876-fa8b0c29f4a1",
      "timezone": "Asia/Kolkata",
      "offset": "UTC+05:30"
    },
    "language": {
      "id": "f516a24b-9e4b-4b2a-88fa-10a905a96db2",
      "name": "English (India)",
      "code": "en-IN"
    },
    "tenant": {
      "id": "b0a2c9f4-53c2-4091-9876-fa8b0c29f4a1",
      "name": "Acme Corp",
      "subdomain": "acme"
    },
    "subscription": {
      "id": "e4f8d2b6-dc94-4ab2-b258-2945a0b7cb2a",
      "status": "active",
      "status_display": "Active",
      "start_date": "2026-06-03T10:00:00Z",
      "end_date": "2027-06-03T10:00:00Z",
      "plan_snapshot": {
        "id": "a9a3b8d4-53c2-4091-9876-fa8b0c29f4a1",
        "code": "premium-yearly",
        "name": "Premium Plan",
        "description": "Premium annual plan",
        "base_price": "100.00",
        "discount_percentage": "0.00",
        "discount_amount": "0.00",
        "net_base_price": "100.00",
        "tax_percentage": "0.00",
        "tax_amount": "0.00",
        "total_price": "100.00",
        "currency": "USD",
        "billing_cycle": "yearly",
        "plan_type": "paid"
      },
      "plan": {
        "id": "a9a3b8d4-53c2-4091-9876-fa8b0c29f4a1",
        "code": "premium-yearly",
        "name": "Premium Plan",
        "description": "Premium annual plan",
        "base_price": "100.00",
        "discount_percentage": "0.00",
        "discount_amount": "0.00",
        "net_base_price": "100.00",
        "tax_percentage": "0.00",
        "tax_amount": "0.00",
        "total_price": "100.00",
        "currency": "USD",
        "currency_display": "USD",
        "billing_cycle": "yearly",
        "billing_cycle_display": "Yearly",
        "plan_type": "paid",
        "plan_type_display": "Paid",
        "sort_order": 1,
        "is_most_popular": false,
        "is_active": true,
        "created_at": "2026-06-03T09:00:00Z",
        "updated_at": "2026-06-03T09:00:00Z"
      },
      "is_active": true,
      "created_at": "2026-06-03T10:00:00Z",
      "updated_at": "2026-06-03T10:00:00Z"
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

## ➕ 3. Create User

* **Path**: `/api/v1/user/users/`
* **Method**: `POST`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Creates a new user in the system.

### Request Payload

```json
{
  "email": "newuser@example.com",
  "password": "SecurePassword123",
  "first_name": "Jane",
  "last_name": "Smith",
  "phone_number": "+9876543210",
  "session_timeout_hours": 24,
  "timezone": "a9a3b8d4-53c2-4091-9876-fa8b0c29f4a1",
  "language": "f516a24b-9e4b-4b2a-88fa-10a905a96db2"
}
```

### Responses

#### 201 Created (Success)

```json
{
  "status": true,
  "message": "Created successfully.",
  "data": {
    "id": "a9a3b8d4-53c2-4091-9876-fa8b0c29f4a1",
    "email": "newuser@example.com",
    "first_name": "Jane",
    "last_name": "Smith",
    "phone_number": "+9876543210",
    "is_active": true,
    "is_verified": false,
    "session_timeout_hours": 24,
    "created_at": "2026-06-03T12:00:00Z",
    "updated_at": "2026-06-03T12:00:00Z",
    "timezone": {
      "id": "a9a3b8d4-53c2-4091-9876-fa8b0c29f4a1",
      "timezone": "Asia/Kolkata",
      "offset": "UTC+05:30"
    },
    "language": {
      "id": "f516a24b-9e4b-4b2a-88fa-10a905a96db2",
      "name": "English (India)",
      "code": "en-IN"
    }
  },
  "errors": null
}
```

#### 400 Bad Request (Validation Error)

```json
{
  "status": false,
  "message": "A user with that email already exists.",
  "data": null,
  "errors": {
    "email": ["A user with that email already exists."]
  }
}
```

---

## 🔄 4. Update User (PUT / PATCH)

* **Path**: `/api/v1/user/users/{id}/`
* **Method**: `PUT` or `PATCH`
* **Authentication**: Bearer JWT (IsAuthenticated)
* **Description**: Updates user profile parameters.

### Request Payload (PATCH example)

```json
{
  "first_name": "Johnny",
  "session_timeout_hours": 24,
  "timezone": "a9a3b8d4-53c2-4091-9876-fa8b0c29f4a1",
  "language": "f516a24b-9e4b-4b2a-88fa-10a905a96db2"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Updated successfully.",
  "data": {
    "id": "c1f7b0f6-dc94-4ab2-b258-2945a0b7cb2a",
    "email": "user@example.com",
    "first_name": "Johnny",
    "last_name": "Doe",
    "phone_number": "+1234567890",
    "is_active": true,
    "is_verified": true,
    "session_timeout_hours": 24,
    "created_at": "2026-06-03T10:00:00Z",
    "updated_at": "2026-06-03T12:30:00Z",
    "timezone": {
      "id": "a9a3b8d4-53c2-4091-9876-fa8b0c29f4a1",
      "timezone": "Asia/Kolkata",
      "offset": "UTC+05:30"
    },
    "language": {
      "id": "f516a24b-9e4b-4b2a-88fa-10a905a96db2",
      "name": "English (India)",
      "code": "en-IN"
    }
  },
  "errors": null
}
```

#### 404 Not Found

```json
{
  "status": false,
  "message": "User not found.",
  "data": null,
  "errors": ["NOT_FOUND"]
}
```

---

## ❌ 5. Delete User

* **Path**: `/api/v1/user/users/{id}/`
* **Method**: `DELETE`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Soft-deletes a user from the system.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "User deleted successfully.",
  "data": null,
  "errors": null
}
```

#### 404 Not Found

```json
{
  "status": false,
  "message": "User not found.",
  "data": null,
  "errors": ["NOT_FOUND"]
}
```

---

## 🔒 6. Change Password

* **Path**: `/api/v1/auth/users/change-password/`
* **Method**: `POST`
* **Authentication**: Bearer JWT (IsAuthenticated)
* **Description**: Allows an authenticated user to change their current password.

### Request Payload

```json
{
  "old_password": "SecurePassword123!",
  "new_password": "NewSecurePassword123!",
  "confirm_password": "NewSecurePassword123!"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Password changed successfully.",
  "data": null,
  "errors": null
}
```

#### 400 Bad Request (Validation Errors)

* **Edge Case 1**: Old password is incorrect.
* **Edge Case 2**: `new_password` and `confirm_password` do not match.
* **Edge Case 3**: New password is weak or invalid (e.g. contains spaces, missing numbers/uppercase/lowercase/special character).

```json
{
  "status": false,
  "message": "Validation failed.",
  "data": null,
  "errors": {
    "confirm_password": [
      "New passwords do not match."
    ]
  }
}
```

