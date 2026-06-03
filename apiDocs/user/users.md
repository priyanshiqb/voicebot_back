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
        "username": "user123",
        "first_name": "John",
        "last_name": "Doe",
        "phone_number": "+1234567890",
        "is_active": true,
        "is_verified": true,
        "created_at": "2026-06-03T10:00:00Z",
        "updated_at": "2026-06-03T10:00:00Z"
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
    "username": "user123",
    "first_name": "John",
    "last_name": "Doe",
    "phone_number": "+1234567890",
    "is_active": true,
    "is_verified": true,
    "created_at": "2026-06-03T10:00:00Z",
    "updated_at": "2026-06-03T10:00:00Z"
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
  "username": "newuser123",
  "password": "SecurePassword123",
  "first_name": "Jane",
  "last_name": "Smith",
  "phone_number": "+9876543210"
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
    "username": "newuser123",
    "first_name": "Jane",
    "last_name": "Smith",
    "phone_number": "+9876543210",
    "is_active": true,
    "is_verified": false,
    "created_at": "2026-06-03T12:00:00Z",
    "updated_at": "2026-06-03T12:00:00Z"
  },
  "errors": null
}
```

#### 400 Bad Request (Validation Error)

```json
{
  "status": false,
  "message": "A user with that username already exists.",
  "data": null,
  "errors": {
    "username": ["A user with that username already exists."]
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
  "first_name": "Johnny"
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
    "username": "user123",
    "first_name": "Johnny",
    "last_name": "Doe",
    "phone_number": "+1234567890",
    "is_active": true,
    "is_verified": true,
    "created_at": "2026-06-03T10:00:00Z",
    "updated_at": "2026-06-03T12:30:00Z"
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
