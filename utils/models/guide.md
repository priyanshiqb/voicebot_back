# Developer Guide: Global Base Models

This package provides global, standardized abstract base models that automatically manage entity primary keys, auditing timestamps, soft deletion, and status fields across the system.

---

## 1. Available Base Models

| Base Model | Description | Inherited Attributes |
| :--- | :--- | :--- |
| **`BaseModelCore`** | Core system database configuration without auditing references (useful for decoupled/auth classes). | `id` (UUIDv4), `created_at`, `updated_at`, `deleted_at`, `is_deleted` |
| **`BaseModel`** | Enterprise-grade fully audited base class mapping user creation/modification details. | `id` (UUIDv4), `created_at`, `updated_at`, `deleted_at`, `is_deleted`, `created_by` (FK), `updated_by` (FK), `deleted_by` (FK) |

---

## 2. Quick Import & Setup Steps

Import directly from the unified **`utils.models`** entrypoint:

```python
from django.db import models
from utils.models import BaseModel # Fully audited standard base class
```

### Step-by-Step Example

To create a standard, fully audited database table:

```python
from django.db import models
from utils.models import BaseModel

class Contact(BaseModel):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)

    class Meta(BaseModel.Meta):
        verbose_name = "Contact"
        verbose_name_plural = "Contacts"

    def __str__(self):
        return f"{self.first_name} {self.last_name}"
```

---

## 3. Key Core Behaviors

1. **Auto-UUID IDs**: Automatically specifies the primary key as a non-editable UUIDv4.
2. **Standardized Audits**: Ensures that every model inherits uniform timestamps (`created_at`, `updated_at`, `deleted_at`).
3. **Soft Deletion Lifecycle**: Subclasses inherit `.soft_delete()` and `.restore()` commands, setting `is_deleted=True` instead of erasing the DB row.
