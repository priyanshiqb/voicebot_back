# Admin2 (District) Dropdown API

This API provides a list of second-level administrative divisions (Districts/Counties) for a specific state (Admin1).

## Endpoint

`GET /api/v1/onboard/admin2/dropdown/`

## Query Parameters

| Parameter   | Type   | Required | Description                                                  |
| :---------- | :----- | :------- | :----------------------------------------------------------- |
| `admin1_id` | UUID   | **Yes**  | Filter by parent state (Admin1) UUID.                        |
| `name`      | String | No       | Filter exactly by district name.                             |
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
            "value": "123e4567-e89b-12d3-a456-426614174002",
            "label": "Los Angeles"
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

### No Districts Found (400 Bad Request)

Returned when the selected state has no registered districts, indicating the frontend should skip to City selection.

```json
{
    "status": false,
    "message": "No districts found for this state",
    "data": null,
    "errors": {
        "code": "NO_FURTHER_DATA"
    }
}
```
