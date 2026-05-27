# Developer Guide: Global Base Serializers

This package provides custom base serializers and relation fields to standardize model serialization, protect systemic attributes, and optimize object references during reads and writes.

---

## 1. Available Base Serializers

| Class Name | Description | Key Features |
| :--- | :--- | :--- |
| **`BaseModelCoreSerializer`** | Core model serializer that automatically marks fundamental system fields as `read_only`. | Protects `id`, `created_at`, `updated_at`, `deleted_at`, `is_active`, `is_deleted` |
| **`BaseModelSerializer`** | Enterprise-grade fully audited serializer subclassing `BaseModelCoreSerializer`. | Auto-injects nested audited users mapping (`created_by`, `updated_by`, `deleted_by`) using `HybridRelatedField` |

---

## 2. Dynamic Relations: `HybridRelatedField`

`HybridRelatedField` is a highly efficient custom field that inherits from `PrimaryKeyRelatedField`. 

### The Problem it Solves:
Usually, in DRF, you have to write separate serializers for reading (nested objects) and writing (passing simple primary key IDs). 

### The Solution:
`HybridRelatedField` behaves as a **simple integer/UUID ID key** during incoming writes (validation/POST), but automatically converts the reference to a **fully nested JSON representation** during outgoing reads (GET)!

---

## 3. Quick Import & Setup Steps

```python
from utils.serializers import BaseModelSerializer, HybridRelatedField
from my_app.models import Contact
from other_app.serializers import TagSerializer
```

### Step-by-Step Example

To serialize a fully audited model with a hybrid relation:

```python
from utils.serializers import BaseModelSerializer, HybridRelatedField
from my_app.models import Contact
from other_app.models import Tag
from other_app.serializers import TagRefSerializer

class ContactSerializer(BaseModelSerializer):
    # GET returns fully nested TagRefSerializer representation.
    # POST/PUT accepts simple UUID tag primary keys!
    tag = HybridRelatedField(
        queryset=Tag.objects.all(),
        serializer=TagRefSerializer,
        required=False,
    )

    class Meta(BaseModelSerializer.Meta):
        model = Contact
        fields = (
            "id",
            "first_name",
            "last_name",
            "email",
            "tag",
            "created_by",      # Inherited read-only
            "updated_by",      # Inherited read-only
        )
```
