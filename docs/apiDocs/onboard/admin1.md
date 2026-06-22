# Admin1 (State) Dropdown API

This API provides a list of first-level administrative divisions (States/Provinces) for a specific country.

## Endpoint

`GET /api/v1/onboard/admin1/dropdown/`

## Query Parameters

| Parameter    | Type   | Required | Description                                                  |
| :----------- | :----- | :------- | :----------------------------------------------------------- |
| `country_id` | UUID   | **Yes**  | Filter by parent country UUID.                               |
| `name`       | String | No       | Filter exactly by state name.                                |
| `search`     | String | No       | Partial case-insensitive search on the `name` field.         |
| `page`       | Int    | No       | Page number for pagination (default: 1).                     |
| `page_size`  | Int    | No       | Number of items per page (default: 10, max: 25).             |

## Responses

### Success (200 OK)

```json
{
    "status": true,
    "message": "success",
    "data": [
        {
            "value": "123e4567-e89b-12d3-a456-426614174001",
            "label": "California"
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

### No States Found (400 Bad Request)

Returned when the selected country has no registered states, indicating the frontend should skip to City selection.

```json
{
    "status": false,
    "message": "No states found for this country",
    "data": null,
    "errors": {
        "code": "NO_FURTHER_DATA"
    }
}
```

### Missing Parameter (400 Bad Request)

```json
{
    "status": false,
    "message": "country_id parameter is required",
    "data": null,
    "errors": {
        "code": "VALIDATION_ERROR"
    }
}
```
