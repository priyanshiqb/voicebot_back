# Onboarding Languages API

This document covers endpoints related to listing, creating, retrieving, updating, deleting, and fetching dropdown pickers of system languages.

---

## 🔍 1. List Languages

* **Path**: `/api/v1/onboard/languages/`
* **Method**: `GET`
* **Authentication**: Bearer JWT (IsAuthenticated)
* **Description**: Returns a paginated list of languages.

### Query Parameters

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `is_active` | Boolean | No | Filter by active state. |
| `code` | String | No | Filter by exact language code (e.g. `en-US`). |
| `name` | String | No | Filter by exact name; use `name__icontains` for partial matches. |
| `created_at__date` / `created_at__gte` / `created_at__lte` | Date | No | Filter by creation date. |
| `search` | String | No | Search text matching language name or code. |
| `ordering` | String | No | Order by `name`, `code`, or `created_at` (prefix `-` for descending). |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "f516a24b-9e4b-4b2a-88fa-10a905a96db2",
      "name": "English (US)",
      "code": "en-US",
      "is_active": true,
      "created_at": "2026-06-05T10:14:00.000000Z"
    }
  ],
  "errors": null,
  "meta": {
    "count": 48,
    "next": null,
    "previous": null,
    "page": 1,
    "page_size": 10
  }
}
```

---

## ➕ 2. Create Language

* **Path**: `/api/v1/onboard/languages/`
* **Method**: `POST`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Creates a new language configuration.

### Request Payload

```json
{
  "name": "French (France)",
  "code": "fr-FR",
  "is_active": true
}
```

### Responses

#### 201 Created (Success)

```json
{
  "status": true,
  "message": "Created successfully",
  "data": {
    "id": "f516a24b-9e4b-4b2a-88fa-10a905a96db2",
    "name": "French (France)",
    "code": "fr-FR",
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
    "code": ["Language with this code already exists."]
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

## 👤 3. Get Language Details

* **Path**: `/api/v1/onboard/languages/{id}/`
* **Method**: `GET`
* **Authentication**: Bearer JWT (IsAuthenticated)
* **Description**: Retrieve detailed profile of a language by ID.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "id": "f516a24b-9e4b-4b2a-88fa-10a905a96db2",
    "name": "French (France)",
    "code": "fr-FR",
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
  "message": "Language not found.",
  "data": null,
  "errors": ["NOT_FOUND"]
}
```

---

## 🔄 4. Update Language (PUT / PATCH)

* **Path**: `/api/v1/onboard/languages/{id}/`
* **Method**: `PUT` or `PATCH`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Updates language parameters.

### Request Payload (PATCH example)

```json
{
  "name": "French Standard"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Updated successfully",
  "data": {
    "id": "f516a24b-9e4b-4b2a-88fa-10a905a96db2",
    "name": "French Standard",
    "code": "fr-FR",
    "is_active": true,
    "created_at": "2026-06-05T10:14:00.000000Z",
    "updated_at": "2026-06-05T10:15:30.000000Z"
  },
  "errors": null
}
```

---

## ❌ 5. Delete Language

* **Path**: `/api/v1/onboard/languages/{id}/`
* **Method**: `DELETE`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Soft-deletes a language.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Deleted successfully",
  "data": null,
  "errors": null
}
```

---

## 🔌 6. Get Dropdown Options

* **Path**: `/api/v1/onboard/languages/dropdown/`
* **Method**: `GET`
* **Authentication**: Bearer JWT (IsAuthenticated)
* **Description**: Returns lightweight label/value selection pairs representing all active languages to populate picker lists.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Dropdown options retrieved successfully.",
  "data": [
    {
      "label": "English (US) (en-US)",
      "value": "f516a24b-9e4b-4b2a-88fa-10a905a96db2"
    },
    {
      "label": "English (India) (en-IN)",
      "value": "ae601d3a-23d2-45e0-9467-34fa88d9ab04"
    }
  ],
  "errors": null
}
```

---

## 🗑️ 7. Bulk Delete Languages

* **Path**: `/api/v1/onboard/languages/bulk_delete/`
* **Method**: `POST`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Bulk soft-deletes the listed languages.
* **Behaviour**: Soft-deletes listed rows; if any id is invalid the whole request fails with 400 and nothing is deleted.

### Request Payload

```json
{
  "ids": ["f516a24b-9e4b-4b2a-88fa-10a905a96db2", "ae601d3a-23d2-45e0-9467-34fa88d9ab04"]
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Bulk deleted successfully",
  "data": null,
  "errors": null
}
```

#### 400 Bad Request (Invalid IDs)

```json
{
  "status": false,
  "message": "Some ids are invalid",
  "data": null,
  "errors": null
}
```
