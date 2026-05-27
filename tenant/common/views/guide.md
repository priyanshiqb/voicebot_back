# Developer Guide: Reusable Tenant Base ViewSets

This package provides a secure, reusable base ViewSet class to automatically partition your REST API endpoints by tenant context and auto-inject the client's mapped tenant context during creation events.

---

## 1. Available Base ViewSet

| Class Name | Description | Key Features |
| :--- | :--- | :--- |
| **`TenantBaseViewSet`** | Enterprise-grade, row-level API isolation base class inheriting from global `BaseViewSet`. | Queryset filtering + create auto-injection + superuser bypass |

---

## 2. Quick Import & Setup Steps

Import directly from the unified **`tenant.common`** entrypoint:

```python
from tenant.common import TenantBaseViewSet # Imports tenant isolated API ViewSet
from my_app.models import Voicebot
from my_app.serializers import VoicebotSerializer
```

### Step-by-Step Example

To partition a REST API resource, subclass it from `TenantBaseViewSet`:

```python
from tenant.common import TenantBaseViewSet
from my_app.models import Voicebot
from my_app.serializers import VoicebotSerializer

class VoicebotViewSet(TenantBaseViewSet):
    queryset = Voicebot.objects.all()
    serializer_class = VoicebotSerializer

    # Optional search, sort, and filtering configurations:
    search_fields = ["name"]
    filterset_fields = {
        "is_active": ["exact"],
    }
```

---

## 3. Key API Security Protections

1. **Automatic Queryset Partitioning**: Overrides `get_queryset()` to ensure standard logged-in users only receive records matching `user.tenant_mapping.tenant`. Django Superusers (`is_superuser`) automatically bypass filters to view all records globally.
2. **Context Auto-Injection (`perform_create`)**: Intercepts record creation requests to automatically resolve the user's mapped `Tenant` reference and inject it into the serializer during execution:
   ```python
   # No need to send 'tenant' key in the POST body! It is securely mapped server-side.
   ```
3. **Robust Security Exceptions**: Automatically raises a REST `PermissionDenied` error if a user lacks a valid mapped tenant configuration, preventing orphan record generation.
