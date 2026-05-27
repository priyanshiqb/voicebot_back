# Developer Guide: Reusable Tenant Base Admins

This package provides highly secure, reusable base classes to enforce row-level tenant security isolation and contextual auto-assignment directly in the **Django Admin** control panel.

---

## 1. Available Base Admins

| Base Admin Class | Description | Standard Admin Constraints |
| :--- | :--- | :--- |
| **`TenantBaseAdminCore`** | Exposes basic auditing read-only details without direct auditing models setup. | Row-level queryset filtering + read-only `tenant` assignment |
| **`TenantBaseAdmin`** | Fully audited administration covering user trackers (`created_by`, `updated_by`, `deleted_by`). | Row-level isolation + contextual saves + audited users links mapping |

---

## 2. Quick Import & Setup Steps

Import directly from the unified **`tenant.common`** entrypoint:

```python
from django.contrib import admin
from tenant.common import TenantBaseAdmin # Imports fully audited base admin
from my_app.models import Voicebot
```

### Step-by-Step Example

To register a tenant-scoped model in the admin console with built-in isolation, extend `TenantBaseAdmin`:

```python
from django.contrib import admin
from tenant.common import TenantBaseAdmin
from my_app.models import Voicebot

@admin.register(Voicebot)
class VoicebotAdmin(TenantBaseAdmin):
    list_display = (
        "id",
        "name",
        "tenant",          # Inherited FK
        "created_at",
        "is_active",
    )

    search_fields = (
        "name",
    )

    # Optional: Customize additional fieldsets while merging base readonly_fields
```

---

## 3. Key Administrative Security Protections

1. **Row-Level Queryset Filtering**: Restricts querysets so standard tenant administrators **only** view records matching their mapped `user.tenant_mapping.tenant`. Django Superusers (`is_superuser`) automatically bypass filters to manage all data globally.
2. **Context Auto-Injection**: Standard users cannot select or edit the `tenant` relationship; it is auto-injected from the active login session during `save_model`.
3. **Immutability Enforcement**: The `tenant` ForeignKey is dynamically set as `readonly` for standard administrators, preventing unauthorized cross-tenant operations.
