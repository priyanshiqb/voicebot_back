# Onboarding Timezones API

This document covers endpoints related to listing, creating, retrieving, updating, deleting, and fetching dropdown pickers of global timezones.

---

## 🔍 1. List Timezones

* **Path**: `/api/v1/onboard/timezones/`
* **Method**: `GET`
* **Authentication**: None (Anonymous)
* **Description**: Returns a paginated list of global timezones.

### Query Parameters

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `is_active` | Boolean | No | Filter by active state. |
| `offset` | String | No | Filter by exact offset (e.g. `UTC+05:30`). |
| `region` | String | No | Filter or search by region/cities (e.g. `London`, `Mumbai`). |
| `search` | String | No | Search text matching timezone identifier, offset, or region. |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "List retrieved successfully.",
  "data": {
    "count": 37,
    "next": null,
    "previous": null,
    "results": [
      {
        "id": "e305e94b-9e4b-4b2a-88fa-10a905a96ab1",
        "offset": "UTC+05:30",
        "region": "Mumbai, Delhi, Kolkata, Colombo",
        "timezone": "Asia/Kolkata",
        "is_active": true,
        "created_at": "2026-06-05T10:14:00.000000Z"
      }
    ]
  },
  "errors": null
}
```

---

## ➕ 2. Create Timezone

* **Path**: `/api/v1/onboard/timezones/`
* **Method**: `POST`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Creates a new timezone configuration.

### Request Payload

```json
{
  "offset": "UTC+05:30",
  "region": "Mumbai, Delhi, Kolkata, Colombo",
  "timezone": "Asia/Kolkata",
  "is_active": true
}
```

### Responses

#### 201 Created (Success)

```json
{
  "status": true,
  "message": "Created successfully.",
  "data": {
    "id": "e305e94b-9e4b-4b2a-88fa-10a905a96ab1",
    "offset": "UTC+05:30",
    "region": "Mumbai, Delhi, Kolkata, Colombo",
    "timezone": "Asia/Kolkata",
    "is_active": true,
    "created_at": "2026-06-05T10:14:00.000000Z",
    "updated_at": "2026-06-05T10:14:00.000000Z"
  },
  "errors": null
}
```

#### 400 Bad Request

```json
{
  "status": false,
  "message": "Validation failed.",
  "data": null,
  "errors": {
    "timezone": ["Timezone with this timezone already exists."]
  }
}
```

#### 403 Forbidden

```json
{
  "status": false,
  "message": "You do not have permission to perform this action.",
  "data": null,
  "errors": ["PERMISSION_DENIED"]
}
```

---

## 👤 3. Get Timezone Details

* **Path**: `/api/v1/onboard/timezones/{id}/`
* **Method**: `GET`
* **Authentication**: None (Anonymous)
* **Description**: Retrieve detailed profile of a timezone by ID.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Details retrieved successfully.",
  "data": {
    "id": "e305e94b-9e4b-4b2a-88fa-10a905a96ab1",
    "offset": "UTC+05:30",
    "region": "Mumbai, Delhi, Kolkata, Colombo",
    "timezone": "Asia/Kolkata",
    "is_active": true,
    "created_at": "2026-06-05T10:14:00.000000Z",
    "updated_at": "2026-06-05T10:14:00.000000Z"
  },
  "errors": null
}
```

#### 404 Not Found

```json
{
  "status": false,
  "message": "Timezone not found.",
  "data": null,
  "errors": ["NOT_FOUND"]
}
```

---

## 🔄 4. Update Timezone (PUT / PATCH)

* **Path**: `/api/v1/onboard/timezones/{id}/`
* **Method**: `PUT` or `PATCH`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Updates timezone parameters.

### Request Payload (PATCH example)

```json
{
  "region": "Mumbai, Delhi, Kolkata, Colombo, Chennai"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Updated successfully.",
  "data": {
    "id": "e305e94b-9e4b-4b2a-88fa-10a905a96ab1",
    "offset": "UTC+05:30",
    "region": "Mumbai, Delhi, Kolkata, Colombo, Chennai",
    "timezone": "Asia/Kolkata",
    "is_active": true,
    "created_at": "2026-06-05T10:14:00.000000Z",
    "updated_at": "2026-06-05T10:15:30.000000Z"
  },
  "errors": null
}
```

---

## ❌ 5. Delete Timezone

* **Path**: `/api/v1/onboard/timezones/{id}/`
* **Method**: `DELETE`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Soft-deletes a timezone.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Timezone deleted successfully.",
  "data": null,
  "errors": null
}
```

---

## 🔌 6. Get Dropdown Options

* **Path**: `/api/v1/onboard/timezones/dropdown/`
* **Method**: `GET`
* **Authentication**: None (Anonymous)
* **Description**: Returns lightweight label/value selection pairs representing all active timezones to populate picker lists.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Dropdown options retrieved successfully.",
  "data": [
    {
      "label": "Asia/Kolkata (UTC+05:30)",
      "value": "e305e94b-9e4b-4b2a-88fa-10a905a96ab1"
    },
    {
      "label": "UTC (UTC+00:00)",
      "value": "db601d3a-23d2-45e0-9467-34fa88d9ab04"
    }
  ],
  "errors": null
}
```
