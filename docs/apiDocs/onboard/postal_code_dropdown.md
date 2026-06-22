# Postal Code Dropdown API

This document outlines the API available for retrieving unique postal codes.

---

## Postal Code Dropdown API

Returns a list of unique postal codes from the system. Supports dynamic space-ignoring searches.

### Endpoint

`GET /api/v1/onboard/places/postal-codes/dropdown/`

### Query Parameters

| Parameter     | Type   | Required | Description                                                  |
| :------------ | :----- | :------- | :----------------------------------------------------------- |
| `search`      | String | No       | Search term. Spaces are dynamically stripped before matching. |
| `postal_code` | String | No       | Exact match for postal code.                                 |
| `page`        | Int    | No       | Page number for pagination (default: 1).                     |
| `page_size`   | Int    | No       | Number of items per page (default: 10, max: 25).             |

### Success Response (200 OK)

```json
{
    "status": true,
    "message": "success",
    "data": [
        {
            "value": "395006",
            "label": "395006"
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
