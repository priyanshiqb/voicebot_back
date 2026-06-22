# Onboarding Postal Codes API

This document covers endpoints related to listing, creating, retrieving, updating, deleting, fetching dropdown pickers, and finding the nearest location coordinate of global postal codes.

---

## 🔍 1. List Postal Codes

* **Path**: `/api/v1/onboard/postal-codes/`
* **Method**: `GET`
* **Authentication**: Bearer JWT (IsAuthenticated) or Temporary Token (IsTempUserAuthenticated)
* **Description**: Returns a paginated list of global postal codes. Caps `page_size` at a hard limit of 100 records, and protects database performance with a 2-second query timeout.

### Query Parameters

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `is_active` | Boolean | No | Filter by active state (`true` or `false`). |
| `country_code` | String | No | Filter by exact country code (e.g., `AE`, `IN`). |
| `country_code__icontains` | String | No | Case-insensitive substring match on country code. |
| `postal_code` | String | No | Filter by exact postal code (space-insensitive, e.g., `3286798458` matches `32867 98458`). |
| `place_name` | String | No | Filter by exact place name (e.g., `Al Wuheida`). |
| `place_name__icontains` | String | No | Case-insensitive substring match on place name (e.g., `wuheida`). |
| `search` | String | No | Multi-field search term matching postal code (space-insensitive), place name, country code, or admin name 1. |
| `page` | Integer | No | Page number for pagination. |
| `page_size` | Integer | No | Number of records per page (capped at 100). |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "List retrieved successfully.",
  "data": {
    "count": 2,
    "next": null,
    "previous": null,
    "results": [
      {
        "id": "e305e94b-9e4b-4b2a-88fa-10a905a96ab1",
        "country_code": "AE",
        "postal_code": "32867 98458",
        "place_name": "Al Wuheida",
        "admin_name1": "Dubai",
        "admin_code1": "03",
        "latitude": "25.293300",
        "longitude": "55.339900",
        "is_active": true
      }
    ]
  },
  "errors": null
}
```

---

## ➕ 2. Create Postal Code

* **Path**: `/api/v1/onboard/postal-codes/`
* **Method**: `POST`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Creates a new postal code configuration.

### Request Payload

```json
{
  "country_code": "AE",
  "postal_code": "32867 98458",
  "place_name": "Al Wuheida",
  "admin_name1": "Dubai",
  "admin_code1": "03",
  "admin_name2": "Al Wuheida",
  "admin_code2": "132",
  "latitude": 25.293300,
  "longitude": 55.339900,
  "accuracy": 6,
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
    "country_code": "AE",
    "postal_code": "32867 98458",
    "place_name": "Al Wuheida",
    "admin_name1": "Dubai",
    "admin_code1": "03",
    "admin_name2": "Al Wuheida",
    "admin_code2": "132",
    "admin_name3": null,
    "admin_code3": null,
    "latitude": "25.293300",
    "longitude": "55.339900",
    "accuracy": 6,
    "coordinates": "lat: 25.2933, lon: 55.3399",
    "is_active": true
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
    "latitude": ["Latitude must be between -90 and 90 degrees."],
    "longitude": ["Longitude must be between -180 and 180 degrees."],
    "accuracy": ["Accuracy must be an integer between 1 and 6."]
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

## 👤 3. Get Postal Code Details

* **Path**: `/api/v1/onboard/postal-codes/{id}/`
* **Method**: `GET`
* **Authentication**: Bearer JWT (IsAuthenticated) or Temporary Token (IsTempUserAuthenticated)
* **Description**: Retrieve detailed parameters of a postal code by ID.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Details retrieved successfully.",
  "data": {
    "id": "e305e94b-9e4b-4b2a-88fa-10a905a96ab1",
    "country_code": "AE",
    "postal_code": "32867 98458",
    "place_name": "Al Wuheida",
    "admin_name1": "Dubai",
    "admin_code1": "03",
    "admin_name2": "Al Wuheida",
    "admin_code2": "132",
    "admin_name3": null,
    "admin_code3": null,
    "latitude": "25.293300",
    "longitude": "55.339900",
    "accuracy": 6,
    "coordinates": "lat: 25.2933, lon: 55.3399",
    "is_active": true
  },
  "errors": null
}
```

#### 404 Not Found

```json
{
  "status": false,
  "message": "Postal code not found.",
  "data": null,
  "errors": ["NOT_FOUND"]
}
```

---

## 🔄 4. Update Postal Code (PUT / PATCH)

* **Path**: `/api/v1/onboard/postal-codes/{id}/`
* **Method**: `PUT` or `PATCH`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Updates postal code parameters.

### Request Payload (PATCH example)

```json
{
  "place_name": "Al Wuheida (New District)",
  "latitude": 25.293400
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
    "country_code": "AE",
    "postal_code": "32867 98458",
    "place_name": "Al Wuheida (New District)",
    "admin_name1": "Dubai",
    "admin_code1": "03",
    "admin_name2": "Al Wuheida",
    "admin_code2": "132",
    "admin_name3": null,
    "admin_code3": null,
    "latitude": "25.293400",
    "longitude": "55.339900",
    "accuracy": 6,
    "coordinates": "lat: 25.2934, lon: 55.3399",
    "is_active": true
  },
  "errors": null
}
```

---

## ❌ 5. Delete Postal Code

* **Path**: `/api/v1/onboard/postal-codes/{id}/`
* **Method**: `DELETE`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Deletes a postal code record.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Postal code deleted successfully.",
  "data": null,
  "errors": null
}
```

---

## 🔌 6. Get Dropdown Options

* **Path**: `/api/v1/onboard/postal-codes/dropdown/`
* **Method**: `GET`
* **Authentication**: Bearer JWT (IsAuthenticated) or Temporary Token (IsTempUserAuthenticated)
* **Description**: Returns lightweight label/value selection pairs representing active postal codes. Enforces a hard maximum limit of 100 records and applies a 2-second query timeout.

### Query Parameters

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `limit` | Integer | No | Maximum number of records to return (defaults to 100, maximum allowed value is 100). |
| `value_field` | String | No | The model field to return as the `"value"` in selection pairs (e.g. `postal_code`, defaults to `id`). |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Dropdown options retrieved successfully.",
  "data": [
    {
      "label": "32867 98458 - Al Wuheida (AE)",
      "value": "e305e94b-9e4b-4b2a-88fa-10a905a96ab1",
      "id": "e305e94b-9e4b-4b2a-88fa-10a905a96ab1"
    }
  ],
  "errors": null
}
```

---

## 📍 7. Get Nearest Postal Code

* **Path**: `/api/v1/onboard/postal-codes/nearest/`
* **Method**: `GET`
* **Authentication**: Bearer JWT (IsAuthenticated) or Temporary Token (IsTempUserAuthenticated)
* **Description**: Find the closest active postal code to the target geographic coordinates using Spherical Law of Cosines distance calculation.

### Query Parameters

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `latitude` | Decimal | Yes | Latitude of search point (must be between -90 and 90, e.g., `25.2900`). |
| `longitude` | Decimal | Yes | Longitude of search point (must be between -180 and 180, e.g., `55.3400`). |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Details retrieved successfully.",
  "data": {
    "id": "e305e94b-9e4b-4b2a-88fa-10a905a96ab1",
    "country_code": "AE",
    "postal_code": "32867 98458",
    "place_name": "Al Wuheida",
    "admin_name1": "Dubai",
    "admin_code1": "03",
    "admin_name2": "Al Wuheida",
    "admin_code2": "132",
    "admin_name3": null,
    "admin_code3": null,
    "latitude": "25.293300",
    "longitude": "55.339900",
    "accuracy": 6,
    "coordinates": "lat: 25.2933, lon: 55.3399",
    "is_active": true
  },
  "errors": null
}
```

#### 400 Bad Request (Missing parameters / invalid numbers or ranges)

```json
{
  "status": false,
  "message": "Validation failed.",
  "data": null,
  "errors": {
    "detail": "Both latitude and longitude query parameters are required."
  }
}
```

#### 404 Not Found (No records with coordinates are available in the database)

```json
{
  "status": false,
  "message": "No active postal codes with coordinates found in the system.",
  "data": null,
  "errors": ["NOT_FOUND"]
}
```

#### 400 Bad Request (Query Timeout)
If a search query takes too long or is too broad (exceeding the 2-second timeout for list/dropdown or 3-second timeout for nearest), the API will abort execution and return:

```json
{
  "status": false,
  "message": "Database query timed out. Please refine your search filters to narrow down results.",
  "data": null,
  "errors": {
    "detail": "Database query timed out. Please refine your search filters to narrow down results."
  }
}
```

