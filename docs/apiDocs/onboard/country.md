# Country Dropdown API

This API provides a list of countries for dropdown selection, with built-in search and filtering capabilities.

## Endpoint

`GET /api/v1/onboard/countries/dropdown/`

## Query Parameters

| Parameter | Type   | Required | Description                                                  |
| :-------- | :----- | :------- | :----------------------------------------------------------- |
| `code`    | String | No       | Filter exactly by country ISO code (e.g., `IN`, `US`)        |
| `name`    | String | No       | Filter exactly by country name                               |
| `search`  | String | No       | Partial case-insensitive search on the `name` field          |
| `page`    | Int    | No       | Page number for pagination (default: 1)                      |
| `page_size`| Int   | No       | Number of items per page (default: 10, max: 25)              |

## Responses

### Success (200 OK)

```json
{
    "status": true,
    "message": "success",
    "data": [
        {
            "value": "123e4567-e89b-12d3-a456-426614174000",
            "label": "India",
            "dial_code": "+91",
            "format_mask": "XXXXX XXXXX"
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
