# Postal Code Dropdown API

This document outlines the API available for retrieving unique postal codes.

---

## Postal Code Dropdown API

Returns a dropdown-ready list of places matching a specific postal code. Supports dynamic space-ignoring searches.

> [!NOTE]
> **Priority Sorting System**:
> 1. **Priority 1**: Exact match (ignoring spaces). E.g. searching "365620" exactly matches "36 56 20".
> 2. **Priority 2**: Starts-with match (ignoring spaces). E.g. searching "365" matches "365620".
> 
> The results are ordered first by priority, then alphanumerically by postal code and place name.

### Endpoint

`GET /api/v1/onboard/places/postal-codes/dropdown/`

### Query Parameters

| Parameter     | Type   | Required | Description                                                  |
| :------------ | :----- | :------- | :----------------------------------------------------------- |
| `search`      | String | No       | Search term. Spaces are dynamically stripped before matching. |
| `postal_code` | String | No       | Exact match for postal code.                                 |
| `country_id`  | UUID   | No       | Filter places by exact Country ID.                           |
| `admin1_id`   | UUID   | No       | Filter places by exact Admin1 (State/Province) ID.           |
| `admin2_id`   | UUID   | No       | Filter places by exact Admin2 (County/District) ID.          |
| `admin3_id`   | UUID   | No       | Filter places by exact Admin3 (City/Municipality) ID.        |
| `page`        | Int    | No       | Page number for pagination (default: 1).                     |
| `page_size`   | Int    | No       | Number of items per page (default: 10, max: 25).             |

### Success Response (200 OK)

```json
{
    "status": true,
    "message": "success",
    "data": [
        {
            "value": "b53f68d7-4b7b-40aa-a51c-34f71a4c90e1",
            "label": "Amreli | 365620"
        },
        {
            "value": "c81d11e1-1b2c-48bb-b32b-11d73a4b91f2",
            "label": "Bagasara | 365620"
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
