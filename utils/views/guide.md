# Developer Guide: Global Base ViewSets

This package provides a standardized **`BaseViewSet`** class to unify REST API CRUD behavior, standard pagination payloads, schema documentation, and robust querying (filtering, search, ordering).

---

## 1. Quick Import & Setup Steps

Import directly from the unified **`utils.views`** entrypoint:

```python
from utils.views import BaseViewSet
from my_app.models import Contact
from my_app.serializers import ContactSerializer
```

### Step-by-Step Example

To implement a clean API resource:

```python
from utils.views import BaseViewSet
from my_app.models import Contact
from my_app.serializers import ContactSerializer

class ContactViewSet(BaseViewSet):
    queryset = Contact.objects.all()
    serializer_class = ContactSerializer

    # Optimize database performance by prefetching relations:
    select_related_fields = ["created_by"]

    # Configure search and ordering fields:
    search_fields = ["first_name", "last_name", "email"]
    ordering_fields = ["created_at", "email"]
    
    # Configure exact lookup parameters:
    filterset_fields = {
        "is_active": ["exact"],
    }
```

---

## 2. Advanced API Querying Features

### A. Pagination Format (`SetPagination`)
Pagination is enabled by default with a standardized response shape. Use query parameters to control size:
- `/api/v1/contacts/?page=2&page_size=25`
- Return format:
  ```json
  {
      "status": true,
      "message": "success",
      "data": [...],
      "meta": {
          "count": 120,
          "next": "url...",
          "previous": null,
          "page": 2,
          "page_size": 25
      }
  }
  ```

### B. Dynamic Search & Ordering Lookups
- **Full Text Search**: `/api/v1/contacts/?search=john`
- **Sorting Fields**: `/api/v1/contacts/?ordering=-created_at` (Prefix `-` for descending, multiple items comma-separated).

### C. Standardized Response Mixins
Provides helper return functions inside your custom views:
- `self.success(data=..., message="Operation successful")`
- `self.error(message="Operation failed", errors=...)`
