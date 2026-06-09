# Onboarding Plans API

This document covers endpoints related to listing, creating, retrieving, updating, deleting, and fetching dropdown pickers of subscription plans.

---

## 🔍 1. List Plans

* **Path**: `/api/v1/onboard/plans/`
* **Method**: `GET`
* **Authentication**: None (Anonymous)
* **Description**: Returns a paginated list of subscription plans.

### Query Parameters

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `is_active` | Boolean | No | Filter by active state. |
| `plan_type` | String | No | Filter by type (e.g. `STARTER`, `PRO`, `ENTERPRISE`). |
| `currency` | String | No | Filter by currency code (e.g. `INR`, `USD`). |
| `search` | String | No | Search text matching plan name or code. |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "List retrieved successfully.",
  "data": {
    "count": 3,
    "next": null,
    "previous": null,
    "results": [
      {
        "id": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0",
        "name": "Starter Plan",
        "code": "starter_plan",
        "price": 49.00,
        "currency": "INR",
        "is_active": true
      }
    ]
  },
  "errors": null
}
```

---

## ➕ 2. Create Plan

* **Path**: `/api/v1/onboard/plans/`
* **Method**: `POST`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Creates a new subscription plan.

### Request Payload

```json
{
  "name": "Pro Plan",
  "code": "pro_plan",
  "price": 99.00,
  "currency": "INR",
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
    "id": "c01824ef-912f-410a-b289-fac0e1a2f4ab",
    "name": "Pro Plan",
    "code": "pro_plan",
    "price": 99.00,
    "currency": "INR",
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
    "code": ["Plan with this code already exists."]
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

## 👤 3. Get Plan Details

* **Path**: `/api/v1/onboard/plans/{id}/`
* **Method**: `GET`
* **Authentication**: None (Anonymous)
* **Description**: Retrieve detailed profile of a plan by ID.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Details retrieved successfully.",
  "data": {
    "id": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0",
    "name": "Starter Plan",
    "code": "starter_plan",
    "price": 49.00,
    "currency": "INR",
    "is_active": true
  },
  "errors": null
}
```

#### 404 Not Found

```json
{
  "status": false,
  "message": "Plan not found.",
  "data": null,
  "errors": ["NOT_FOUND"]
}
```

---

## 🔄 4. Update Plan (PUT / PATCH)

* **Path**: `/api/v1/onboard/plans/{id}/`
* **Method**: `PUT` or `PATCH`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Updates plan parameters.

### Request Payload (PATCH example)

```json
{
  "price": 59.00
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Updated successfully.",
  "data": {
    "id": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0",
    "name": "Starter Plan",
    "code": "starter_plan",
    "price": 59.00,
    "currency": "INR",
    "is_active": true
  },
  "errors": null
}
```

---

## ❌ 5. Delete Plan

* **Path**: `/api/v1/onboard/plans/{id}/`
* **Method**: `DELETE`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Soft-deletes a subscription plan.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Plan deleted successfully.",
  "data": null,
  "errors": null
}
```

---

## 🔌 6. Get Dropdown Options

* **Path**: `/api/v1/onboard/plans/dropdown/`
* **Method**: `GET`
* **Authentication**: None (Anonymous)
* **Description**: Returns lightweight label/value selection pairs representing all active plans to populate picker lists.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Dropdown options retrieved successfully.",
  "data": [
    {
      "label": "Starter Plan",
      "value": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0"
    },
    {
      "label": "Pro Plan",
      "value": "c01824ef-912f-410a-b289-fac0e1a2f4ab"
    }
  ],
  "errors": null
}
```
