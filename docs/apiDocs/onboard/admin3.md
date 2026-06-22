# Admin3 (Sub-district) Dropdown API

This API provides a list of third-level administrative divisions (Sub-districts) for a specific district (Admin2).

## Endpoint

`GET /api/v1/onboard/admin3/dropdown/`

## Query Parameters

| Parameter   | Type   | Required | Description                                                  |
| :---------- | :----- | :------- | :----------------------------------------------------------- |
| `admin2_id` | UUID   | **Yes**  | Filter by parent district (Admin2) UUID.                     |
| `name`      | String | No       | Filter exactly by sub-district name.                         |
| `search`    | String | No       | Partial case-insensitive search on the `name` field.         |
| `page`      | Int    | No       | Page number for pagination (default: 1).                     |
| `page_size` | Int    | No       | Number of items per page (default: 10, max: 25).             |

## Responses

### Success (200 OK)

```json
{
    "status": true,
    "message": "success",
    "data": [
        {
            "value": "123e4567-e89b-12d3-a456-426614174003",
            "label": "Downtown"
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

### No Sub-districts Found (400 Bad Request)

Returned when the selected district has no registered sub-districts, indicating the frontend should skip to City selection.

```json
{
    "status": false,
    "message": "No sub-districts found for this district",
    "data": null,
    "errors": {
        "code": "NO_FURTHER_DATA"
    }
}
```
