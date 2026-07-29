# Availability Profile API

This API provides endpoints to manage per-user weekly working hours (availability profiles). These profiles are tenant-scoped and support nested rules for working blocks (including split shifts).

## Endpoints

### 1. List Availability Profiles
**GET** `/core/availability_profiles/`

Retrieves a paginated list of availability profiles. Uses `AvailabilityProfileListSerializer`.

#### Query Parameters
- `search` (string): Text query to search.
- `is_active` (boolean): Filter by active status.
- `user` (UUID): Filter by the associated user ID.
- `ordering` (string): Default is `-created_at`. Options: `created_at`, `updated_at`.

#### Successful Response (200 OK)
```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": "123e4567-e89b-12d3-a456-426614174000",
      "user": {
        "id": "123e4567-e89b-12d3-a456-426614174001",
        "email": "agent@example.com",
        "first_name": "John",
        "last_name": "Doe"
      },
      "name": "Default",
      "timezone": "123e4567-e89b-12d3-a456-426614174002",
      "is_active": true,
      "created_at": "2026-07-29T10:20:00Z"
    }
  ]
}
```

---

### 2. Create Availability Profile
**POST** `/core/availability_profiles/`

Creates a new availability profile with nested working rules. Uses `AvailabilityProfileSerializer`.

#### Request Body
```json
{
  "user": "123e4567-e89b-12d3-a456-426614174001",
  "timezone": "123e4567-e89b-12d3-a456-426614174002",
  "name": "Default",
  "is_active": true,
  "rules": [
    {
      "weekday": 0,
      "start_time": "09:00:00",
      "end_time": "17:00:00"
    },
    {
      "weekday": 1,
      "start_time": "09:00:00",
      "end_time": "12:00:00"
    },
    {
      "weekday": 1,
      "start_time": "13:00:00",
      "end_time": "17:00:00"
    }
  ]
}
```

#### Explicit Field Validations
- **`user`** (UUID, Required): Validated at runtime. Must belong to the same tenant. A user can only have **one active availability profile** per tenant; otherwise, it returns HTTP 400.
- **`timezone`** (UUID, Required): Must be a valid ID from the `Timezone` model.
- **`rules`** (List of Objects, Optional):
  - **`weekday`**: Integer (0=Monday to 6=Sunday).
  - **`start_time`**, **`end_time`**: Local wall-clock times. `end_time` must be strictly after `start_time`. Overlapping rules on the same `weekday` will trigger a Validation Error.

#### Successful Response (201 Created)
```json
{
  "id": "123e4567-e89b-12d3-a456-426614174000",
  "tenant": {
    "id": "123e4567-e89b-12d3-a456-426614174099",
    "name": "Acme Corp"
  },
  "user": {
    "id": "123e4567-e89b-12d3-a456-426614174001",
    "email": "agent@example.com",
    "first_name": "John",
    "last_name": "Doe"
  },
  "timezone": "123e4567-e89b-12d3-a456-426614174002",
  "name": "Default",
  "rules": [
    {
      "id": "223e4567-e89b-12d3-a456-426614174000",
      "weekday": 0,
      "start_time": "09:00:00",
      "end_time": "17:00:00"
    }
  ],
  "is_active": true,
  "created_by": { ... },
  "updated_by": { ... },
  "deleted_by": null,
  "created_at": "2026-07-29T10:20:00Z",
  "updated_at": "2026-07-29T10:20:00Z",
  "deleted_at": null
}
```

#### Error Handling (400 Bad Request)
```json
{
  "user": [
    "This user already has an availability profile."
  ],
  "rules": [
    "Overlapping availability rules on weekday 1."
  ]
}
```

---

### 3. Retrieve Availability Profile
**GET** `/core/availability_profiles/{id}/`

Retrieves a single availability profile by its UUID. Returns the full structure seen in the `201 Created` response.

---

### 4. Update Availability Profile
**PUT** `/core/availability_profiles/{id}/`
**PATCH** `/core/availability_profiles/{id}/`

Updates an existing profile. If `rules` are provided, the existing rules are fully replaced (sync by full replacement).

#### Request Body (PATCH Example)
```json
{
  "name": "Updated Schedule",
  "rules": [
    {
      "weekday": 0,
      "start_time": "10:00:00",
      "end_time": "15:00:00"
    }
  ]
}
```

#### Successful Response (200 OK)
Follows the same schema as `POST`.

---

### 5. Delete Availability Profile
**DELETE** `/core/availability_profiles/{id}/`

Soft-deletes the availability profile. 

#### Successful Response (204 No Content)
Empty body.

---

### 6. Dropdown Options
**GET** `/core/availability_profiles/dropdown/`

Retrieves a simplified list of profiles for populating UI select pickers. Uses `AvailabilityProfileDropdownSerializer`.

#### Successful Response (200 OK)
```json
[
  {
    "label": "Default",
    "value": "123e4567-e89b-12d3-a456-426614174000"
  }
]
```
