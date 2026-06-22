# Place and Postal Code APIs

This document outlines the APIs available for retrieving places (cities) and their associated postal codes.

---

## Place (City) Dropdown API

This API provides a list of places/cities and their postal codes, filtered by any of the administrative levels.

### Endpoint

`GET /api/v1/onboard/places/dropdown/`

### Query Parameters

| Parameter    | Type   | Required | Description                                                  |
| :----------- | :----- | :------- | :----------------------------------------------------------- |
| `country_id` | UUID   | No       | Filter by parent country UUID.                               |
| `admin1_id`  | UUID   | No       | Filter by parent state (Admin1) UUID.                        |
| `admin2_id`  | UUID   | No       | Filter by parent district (Admin2) UUID.                     |
| `admin3_id`  | UUID   | No       | Filter by parent sub-district (Admin3) UUID.                 |
| `search`     | String | No       | Search term. Matches against `name` (case-insensitive) OR `postal_code` (ignoring spaces). |
| `page`       | Int    | No       | Page number for pagination (default: 1).                     |
| `page_size`  | Int    | No       | Number of items per page (default: 10, max: 25).             |

### Success Response (200 OK)

```json
{
    "status": true,
    "message": "success",
    "data": [
        {
            "id": "123e4567-e89b-12d3-a456-426614174004",
            "cities": "Al Wuheida",
            "postal_code": "32867 98458"
        }
    ],
    "errors": null,
    "meta": {
        "count": 1,
        "next": null,
        "previous": null,
        "page": 1,
        "page_size": 10
    }
}
```

```

---

## Place (City) Detail API

Retrieves the complete details of a single city, including read-only relational representations of its administrative hierarchy.

### Endpoint

`GET /api/v1/onboard/places/<id>/`

### Success Response (200 OK)

```json
{
    "status": true,
    "message": "success",
    "data": {
        "id": "123e4567-e89b-12d3-a456-426614174004",
        "country": {
            "id": "123e4567-e89b-12d3-a456-426614174000",
            "code": "IN",
            "name": "India"
        },
        "admin1": {
            "id": "123e4567-e89b-12d3-a456-426614174001",
            "code": "GJ",
            "name": "Gujarat"
        },
        "admin2": {
            "id": "123e4567-e89b-12d3-a456-426614174002",
            "code": "SR",
            "name": "Surat"
        },
        "admin3": {
            "id": "123e4567-e89b-12d3-a456-426614174003",
            "code": "VD",
            "name": "Varachha"
        },
        "postal_code": "395006",
        "name": "Varachha Road"
    },
    "errors": null
}
```
