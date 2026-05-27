# Developer Guide: Reusable Tenant Base Serializers

This package provides custom base serializers to automatically protect the `tenant` relationship as read-only and automatically serialize the tenant's context as a fully nested object during reads (GET).

---

## 1. Available Base Serializers

| Class Name | Description | Key Features |
| :--- | :--- | :--- |
| **`TenantBaseModelCoreSerializer`** | Core serializer for non-audited multi-tenant models (e.g. settings, system metadata mappings). | Protects `tenant` relation + returns nested details on read |
| **`TenantBaseModelSerializer`** | Full audited serializer subclassing `BaseModelSerializer` to cover user tracking details. | Standard user trackers (`created_by`, `updated_by`, `deleted_by`) + hybrid nested tenant mapping |

---

## 2. Quick Import & Setup Steps

Import directly from the unified **`tenant.common`** entrypoint:

```python
from tenant.common import TenantBaseModelSerializer # Imports audited tenant base serializer
from my_app.models import Voicebot
```

### Step-by-Step Example

To implement an API serializer for a tenant-scoped model (e.g. `Voicebot`), inherit from `TenantBaseModelSerializer`:

```python
from tenant.common import TenantBaseModelSerializer
from my_app.models import Voicebot

class VoicebotSerializer(TenantBaseModelSerializer):
    class Meta(TenantBaseModelSerializer.Meta):
        model = Voicebot
        fields = (
            "id",
            "name",
            "description",
            "tenant",          # Inherited read-only hybrid relation
            "created_by",      # Inherited read-only hybrid user tracking relation
            "updated_by",      # Inherited read-only hybrid user tracking relation
        )
```

---

## 3. Key Multi-Tenant Features

1. **Auto Hybrid Relationship**: The `tenant` field automatically uses `HybridRelatedField` with `TenantRefSerializer`. 
   - **GET API responses** return the fully nested tenant context:
     ```json
     {
         "id": "voicebot-uuid-1",
         "name": "Customer Support Bot",
         "tenant": {
             "id": "tenant-uuid-xyz",
             "name": "Acme Corp",
             "subdomain": "acme"
         }
     }
     ```
   - **POST API payloads** ignore the `tenant` field on validate (since it is read-only). The tenant context is safely injected server-side by the `TenantBaseViewSet.perform_create()` hook!
2. **Standardized Auditing Injections**: Integrates cleanly with standard user-referenced auditing profiles.
