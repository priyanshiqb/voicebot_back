# Developer Guide: Reusable Tenant Base Models

This package provides reusable base models to easily partition database records by organization (`Tenant`) and auto-inherit standardized auditing/soft-delete capabilities.

---

## 1. Available Base Models

| Base Model | Description | Inherited Attributes |
| :--- | :--- | :--- |
| **`TenantBaseModelCore`** | Core row-level multi-tenant context partitioning. Excludes user-audited metadata. | `id` (UUIDv4), `tenant` (FK), `created_at`, `updated_at`, `deleted_at`, `is_deleted` |
| **`TenantBaseModel`** | Complete audited row-level multi-tenant context partitioning. Includes standard user-audited tracking. | `id` (UUIDv4), `tenant` (FK), `created_at`, `updated_at`, `deleted_at`, `is_deleted`, `created_by` (FK), `updated_by` (FK), `deleted_by` (FK) |

---

## 2. Quick Import & Setup Steps

Import directly from the unified **`tenant.common`** entrypoint:

```python
from django.db import models
from tenant.common import TenantBaseModel # Imports fully audited multi-tenant base class
```

### Step-by-Step Example

To create a new database model (e.g. `Voicebot`) scoped under a tenant, subclass it from `TenantBaseModel`:

```python
from django.db import models
from tenant.common import TenantBaseModel

class Voicebot(TenantBaseModel):
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)

    class Meta(TenantBaseModel.Meta):
        verbose_name = "Voicebot"
        verbose_name_plural = "Voicebots"

    def __str__(self):
        return f"{self.name} ({self.tenant.name})"
```

---

## 3. Key Architectural Features

1. **Automatic Foreign Key (FK) Injection**: Both base classes automatically inject the `tenant` field pointing to the `Tenant` model with `on_delete=models.CASCADE`.
2. **Soft Deletion (`is_deleted`)**: Subclasses instantly inherit the `.soft_delete()` and `.restore()` actions without manual configuration.
3. **Circular-Dependency Free**: Decoupled from direct circular database migration loops by using separate metadata mapping tables.
