# Onboarding Plans API

This document covers endpoints related to listing, creating, retrieving, updating, deleting, bulk-deleting, and fetching dropdown pickers of subscription plans.

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
| `code` | String | No | Filter by exact plan code. |
| `plan_type` | String | No | Filter by type (`paid`, `free`, `pay_as_you_go`). |
| `currency` | String | No | Filter by currency code (only `INR` is supported). |
| `billing_cycle` | String | No | Filter by billing cycle (`monthly`, `yearly`). |
| `base_price__gte` / `base_price__lte` | Decimal | No | Filter by base price range. |
| `tax_percentage__gte` / `tax_percentage__lte` | Decimal | No | Filter by tax percentage range. |
| `discount_percentage__gte` / `discount_percentage__lte` | Decimal | No | Filter by discount percentage range. |
| `created_at__date` / `created_at__gte` / `created_at__lte` | Date | No | Filter by creation date. |
| `search` | String | No | Search text matching plan `name`, `code`, or `description`. |
| `ordering` | String | No | Order by `name`, `base_price`, `sort_order`, or `created_at` (prefix `-` for descending). |

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "success",
  "data": [
    {
      "id": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0",
      "code": "starter_inr",
      "name": "Starter Plan",
      "base_price": "2000.00",
      "discount_percentage": "10.00",
      "discount_amount": "200.00",
      "net_base_price": "1800.00",
      "tax_percentage": "18.00",
      "tax_amount": "324.00",
      "total_price": "2124.00",
      "currency": "INR",
      "billing_cycle": "monthly",
      "billing_cycle_display": "Monthly",
      "plan_type": "paid",
      "plan_type_display": "Paid",
      "sort_order": 0,
      "is_most_popular": false,
      "is_active": true,
      "created_at": "2026-06-05T10:14:00.000000Z"
    }
  ],
  "errors": null,
  "meta": {
    "count": 3,
    "next": null,
    "previous": null,
    "page": 1,
    "page_size": 10
  }
}
```

---

## ➕ 2. Create Plan

* **Path**: `/api/v1/onboard/plans/`
* **Method**: `POST`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Creates a new subscription plan. `discount_amount`, `net_base_price`, `tax_amount`, and `total_price` are computed server-side and returned read-only.

### Request Payload

Writable fields: `name`, `description`, `base_price`, `discount_percentage`, `tax_percentage`, `billing_cycle`, `plan_type`, `currency`, `sort_order`, `is_most_popular`, `is_active`, `code`.

```json
{
  "code": "pro_inr",
  "name": "Pro Plan",
  "description": "Advanced plan for growing teams.",
  "base_price": "5000.00",
  "discount_percentage": "0.00",
  "tax_percentage": "18.00",
  "currency": "INR",
  "billing_cycle": "monthly",
  "plan_type": "paid",
  "sort_order": 1,
  "is_most_popular": true,
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
    "id": "c01824ef-912f-410a-b289-fac0e1a2f4ab",
    "code": "pro_inr",
    "name": "Pro Plan",
    "description": "Advanced plan for growing teams.",
    "base_price": "5000.00",
    "discount_percentage": "0.00",
    "discount_amount": "0.00",
    "net_base_price": "5000.00",
    "tax_percentage": "18.00",
    "tax_amount": "900.00",
    "total_price": "5900.00",
    "currency": "INR",
    "currency_display": "Indian Rupee",
    "billing_cycle": "monthly",
    "billing_cycle_display": "Monthly",
    "plan_type": "paid",
    "plan_type_display": "Paid",
    "sort_order": 1,
    "is_most_popular": true,
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
    "code": ["plan with this code already exists."]
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
  "message": "Retrieved successfully",
  "data": {
    "id": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0",
    "code": "starter_inr",
    "name": "Starter Plan",
    "description": "Entry-level plan.",
    "base_price": "2000.00",
    "discount_percentage": "10.00",
    "discount_amount": "200.00",
    "net_base_price": "1800.00",
    "tax_percentage": "18.00",
    "tax_amount": "324.00",
    "total_price": "2124.00",
    "currency": "INR",
    "currency_display": "Indian Rupee",
    "billing_cycle": "monthly",
    "billing_cycle_display": "Monthly",
    "plan_type": "paid",
    "plan_type_display": "Paid",
    "sort_order": 0,
    "is_most_popular": false,
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
* **Description**: Updates plan parameters. Only writable fields are accepted; computed financial fields are recalculated and returned read-only.

### Request Payload (PATCH example)

```json
{
  "base_price": "2500.00"
}
```

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Updated successfully",
  "data": {
    "id": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0",
    "code": "starter_inr",
    "name": "Starter Plan",
    "description": "Entry-level plan.",
    "base_price": "2500.00",
    "discount_percentage": "10.00",
    "discount_amount": "250.00",
    "net_base_price": "2250.00",
    "tax_percentage": "18.00",
    "tax_amount": "405.00",
    "total_price": "2655.00",
    "currency": "INR",
    "currency_display": "Indian Rupee",
    "billing_cycle": "monthly",
    "billing_cycle_display": "Monthly",
    "plan_type": "paid",
    "plan_type_display": "Paid",
    "sort_order": 0,
    "is_most_popular": false,
    "is_active": true,
    "created_at": "2026-06-05T10:14:00.000000Z",
    "updated_at": "2026-06-05T10:15:30.000000Z"
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
  "message": "Deleted successfully",
  "data": null,
  "errors": null
}
```

---

## 🔌 6. Get Dropdown Options

* **Path**: `/api/v1/onboard/plans/dropdown/`
* **Method**: `GET`
* **Authentication**: None (Anonymous)
* **Description**: Returns lightweight label/value selection pairs representing all active plans to populate picker lists. Each row also returns `is_most_popular`.

### Responses

#### 200 OK (Success)

```json
{
  "status": true,
  "message": "Dropdown options retrieved successfully",
  "data": [
    {
      "label": "Starter Plan (Paid - 2124.00 INR/Monthly)",
      "value": "efb14afc-00dc-45c4-ba74-fa9018b0ebb0",
      "is_most_popular": false
    },
    {
      "label": "Pro Plan (Paid - 5900.00 INR/Monthly)",
      "value": "c01824ef-912f-410a-b289-fac0e1a2f4ab",
      "is_most_popular": true
    }
  ],
  "errors": null
}
```

---

## 🗑️ 7. Bulk Delete Plans

* **Path**: `/api/v1/onboard/plans/bulk_delete/`
* **Method**: `POST`
* **Authentication**: Bearer JWT (IsAuthenticated + Superuser permissions)
* **Description**: Bulk soft-deletes the listed plans.
* **Behaviour**: Soft-deletes listed rows; if any id is invalid the whole request fails with 400 and nothing is deleted.

### Request Payload

```json
{
  "ids": ["efb14afc-00dc-45c4-ba74-fa9018b0ebb0", "c01824ef-912f-410a-b289-fac0e1a2f4ab"]
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
