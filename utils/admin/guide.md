# Developer Guide: Global Base Admins

This package provides abstract, reusable administrative console base classes to unify behavior and UI lookups inside the **Django Admin** dashboard.

---

## 1. Available Base Admins

| Class Name | Description | Key Features |
| :--- | :--- | :--- |
| **`BaseAdminCore`** | Core admin setup handling fundamental entity attributes. | Auto readonly fields, default sorting, list filters, soft-delete actions |
| **`BaseAdmin`** | Audited admin subclassing `BaseAdminCore` to track user audit mappings. | Adds user tracks (`created_by`, `updated_by`, `deleted_by`) to autocomplete lists |

---

## 2. Quick Import & Setup Steps

```python
from django.contrib import admin
from utils.admin import BaseAdmin
from my_app.models import Contact
```

### Step-by-Step Example

To register a standard model inside the administration console:

```python
from django.contrib import admin
from utils.admin import BaseAdmin
from my_app.models import Contact

@admin.register(Contact)
class ContactAdmin(BaseAdmin):
    list_display = (
        "id",
        "first_name",
        "last_name",
        "email",
        "is_active",
        "created_at",
    )

    search_fields = (
        "first_name",
        "last_name",
        "email",
    )
```

---

## 3. Standard Global Actions

Both base classes register standard global dropdown action choices:
1. **"Soft delete selected items"**: Automatically triggers the model's `.soft_delete()` lifecycle and saves auditing metadata context.
2. **"Restore selected items"**: Reverts soft-deleted entries to active states.
3. **"Activate/Deactivate selected items"**: Bulk-toggles the `is_active` boolean field on the selected objects.
