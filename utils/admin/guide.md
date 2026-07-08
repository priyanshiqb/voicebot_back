# Developer Guide: Global Base Admins

This package provides abstract, reusable administrative console base classes to unify behavior and UI lookups inside the **Django Admin** dashboard.

---

## 1. Available Base Admins

| Class Name | Description | Key Features |
| :--- | :--- | :--- |
| **`BaseAdminCore`** | Core admin setup handling fundamental entity attributes. | Auto readonly fields, default sorting, list filters, soft-delete actions |
| **`BaseAdmin`** | Audited admin subclassing `BaseAdminCore` to track user audit mappings. | Adds user tracks (`created_by`, `updated_by`, `deleted_by`) to autocomplete lists |

---

## 2. How to Implement & Extend Fields

When inheriting from `BaseAdmin` or `BaseAdminCore`, it is **critical** to properly unpack the base fields so you do not accidentally overwrite the global defaults (like `id`, `created_at`, or auditing fields).

### Properly Adding `readonly_fields`, `autocomplete_fields`, etc.

Use tuple unpacking (`*BaseAdmin.<property>`) when defining these lists in your custom admin classes.

```python
from django.contrib import admin
from utils.admin import BaseAdmin
from my_app.models import Contact

@admin.register(Contact)
class ContactAdmin(BaseAdmin):
    # 1. List Display: Columns shown in the data table
    list_display = (
        "id",
        "first_name",
        "last_name",
        "email",
        "is_active",
        "created_at",
    )

    # 2. Search Fields: What fields the search bar checks
    # BaseAdminCore already searches by 'id'. Add your custom text fields here.
    search_fields = (
        *BaseAdmin.search_fields,
        "first_name",
        "last_name",
        "email",
    )

    # 3. Read-Only Fields: Prevent editing of certain fields.
    # MUST unpack BaseAdmin.readonly_fields to keep 'id', 'created_at', 'created_by', etc. readonly!
    readonly_fields = (
        *BaseAdmin.readonly_fields,
        "system_generated_hash",
    )

    # 4. Autocomplete Fields: Convert standard dropdowns into searchable Select2 inputs.
    # MUST unpack BaseAdmin.autocomplete_fields so audit users ('created_by') remain autocomplete!
    # Note: Any ForeignKey added here MUST have search_fields defined on its own Admin class.
    autocomplete_fields = (
        *BaseAdmin.autocomplete_fields,
        "company",  # Assuming company is a ForeignKey on Contact
    )

    # 5. List Filters: Right-hand sidebar filters
    # Unpack the base filters so you keep 'is_active', 'is_deleted', and 'created_at' filters.
    list_filter = (
        *BaseAdmin.list_filter,
        "contact_type",
    )
```

---

## 3. Standard Global Actions

Both base classes register standard global dropdown action choices:
1. **"Soft delete selected items"**: Automatically triggers the model's `.soft_delete()` lifecycle and saves auditing metadata context.
2. **"Restore selected items"**: Reverts soft-deleted entries to active states.
3. **"Activate/Deactivate selected items"**: Bulk-toggles the `is_active` boolean field on the selected objects.
