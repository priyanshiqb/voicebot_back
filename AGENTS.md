# Voicebot Backend Development & Architectural Standards Guide

Welcome! This guide outlines the core development practices, structural rules, and architectural standards enforced across this Django codebase. 

All AI assistants and developers MUST strictly read, respect, and apply these guidelines on every task.

---

## 🔒 1. Multi-Tenant Architecture & Scoping

To ensure complete tenant isolation and row-level security (RLS), **never** manually re-implement `tenant` fields or filters. Always reuse the core framework defined in **`tenant/common/`**. Depending on whether your model requires full user auditing, select the appropriate tier:

### 🅰️ Tier 1: Core Scoped (Without Direct User Audits setup)
Use these when you need row-level tenant security but do **not** want direct database dependencies on user models (`created_by`, `updated_by`, etc.):

| Component Type | Base Class to Extend | Import Path | Feature Provided |
| :--- | :--- | :--- | :--- |
| **Django Models** | `TenantBaseModelCore` | `tenant.common.models.tenant_base_model_core` | Row-level tenant partitioning, soft-delete, and timestamps |
| **Django Admin** | `TenantBaseAdminCore` | `tenant.common.admin.tenant_base_admin_core` | Queryset tenant filtering, read-only tenant lock, auto-saves |
| **Serializers** | `TenantBaseModelCoreSerializer` | `tenant.common.serializers.tenant_base_model_core_serializer` | Basic serializer with nested read-only Hybrid tenant field |

### 🅱️ Tier 2: Fully Audited (With Active User Audits)
Use these for high-security, user-tracked models requiring accountability logging (adds `created_by`, `updated_by`, `deleted_by` properties automatically):

| Component Type | Base Class to Extend | Import Path | Feature Provided |
| :--- | :--- | :--- | :--- |
| **Django Models** | `TenantBaseModel` | `tenant.common.models.tenant_base_model` | Tier 1 Tenant scoping + fully linked user audit logs |
| **Django Admin** | `TenantBaseAdmin` | `tenant.common.admin.tenant_base_admin` | Tier 1 Admin filtering + automated user audit commits |
| **Serializers** | `TenantBaseModelSerializer` | `tenant.common.serializers.tenant_base_model_serializer` | Tier 1 Serializer + read-only audited user links mapping |

### 🌐 API Views / ViewSets
* **ViewSets**: Subclass `TenantBaseViewSet` (imported from `tenant.common.views.tenant_base_viewset`) for auto-filtering and auto-injecting tenant details.

---

### 🏷️ Normal (Non-Tenant) Models & Services
For standard, global, or non-tenant scoped components (e.g., global plans, general templates, or webhook logging records):
* **Do not** use `tenant/common/` classes.
* Instead, inherit directly from the generic base classes defined inside the **`utils/`** package:

### 🅰️ Tier 1: Core Global (Without Direct User Audits setup)
| Component Type | Base Class to Extend | Import Path | Feature Provided |
| :--- | :--- | :--- | :--- |
| **Django Models** | `BaseModelCore` | `utils.models.base_model_core` | Core ID, is_active, soft-delete hooks, and timestamps |
| **Django Admin** | `BaseAdminCore` | `utils.admin.base_admin_core` | Central ID-based search, list filters, soft-delete actions |
| **Serializers** | `BaseModelCoreSerializer` | `utils.serializers.base_model_core_serializer` | Core serializer with default soft-delete and active state representation |

### 🅱️ Tier 2: Fully Audited Global (With Active User Audits)
| Component Type | Base Class to Extend | Import Path | Feature Provided |
| :--- | :--- | :--- | :--- |
| **Django Models** | `BaseModel` | `utils.models.base_model` | Tier 1 Core + fully linked user auditing (`created_by`, etc.) |
| **Django Admin** | `BaseAdmin` | `utils.admin.base_admin` | Tier 1 Admin + automated user tracking saves |
| **Serializers** | `BaseModelSerializer` | `utils.serializers.base_model_serializer` | Tier 1 Serializer + nested audited user attributes |

### 🌐 API Views / ViewSets
* **ViewSets**: Subclass `BaseViewSet` (imported from `utils.views.base_model_viewset`) for centralized basic view actions and responses.

---

## 🖥️ 2. Django Admin Configuration Rules

### 📦 Package-Based Modular Structure
* Do **not** register multiple admin models inside a single `admin.py` or large file.
* Each Django Model Admin class **MUST** reside in its own dedicated file inside the app's `admin/` package.
* **Naming Standard**: Filenames must be **lowercase snake_case** (e.g., `kb_text_admin.py` or `plan_admin.py`).
* All split admin modules must be imported and exposed inside the admin package's index: `admin/__init__.py`.

### 🔍 Search & UI Features
* **Global ID Search**: All Admin classes must inherit from `BaseAdminCore` (or `TenantBaseAdmin` which extends it), ensuring that `id` search is centrally supported.
* **Clean UI Layouts**: Use `fieldsets` to segment general info, dynamic details, ETL states, and audits. Use `filter_horizontal` for interactive Many-to-Many lists.

---

## ⚙️ 3. Serializer & DRF Guidelines

### 📦 The 4-Tier Serializer Pattern
When writing serializers for a model, implement the following four variants to support standard platform actions:
1. **Ref Serializer** (`*RefSerializer`): Lightweight representation containing only key identifiers, base labels, and codes. Used for nested or read-only relational fields.
2. **Main Serializer** (`*Serializer`): Full detailed representation covering all configuration parameters, dynamic calculations, and active write logic.
3. **List Serializer** (`*ListSerializer`): Optimized layout tailored for list displays, dashboards, and admin tables.
4. **Dropdown Serializer** (`*DropdownSerializer`): Extremely compact structure exposing only `label` and `value` fields to populate picker lists.

### ⛔ Read-Only Relational Fields
* When utilizing relational fields (such as `PrimaryKeyRelatedField` or our custom `HybridRelatedField`) with `read_only=True`, you **MUST NOT** specify a `queryset` argument.
* DRF throws an assertion error if a relational field is configured with both `queryset` and `read_only=True` (since querysets are only required for write-operation validation).
* **Correct Syntax**:
  ```python
  tenant = HybridRelatedField(
      serializer=TenantRefSerializer,
      read_only=True,
  )
  ```

### 🛡️ Serializer Validation & Business Logic Rules
* **Explicit Runtime Validations**: You **MUST** write every single validation that is needed at runtime (including unique constraints, file size/extension checks, domain formats, custom clean methods, etc.). **Do not miss any validation!** Every check must be handled at runtime inside the Serializer before database commit.

---

## 🌐 4. API Views & ViewSets Standards

While implementing any API viewset extending `BaseViewSet` or `TenantBaseViewSet`, you **MUST** analyze the model characteristics and explicitly declare these configuration properties:

| Property | Description | Example |
| :--- | :--- | :--- |
| **`queryset`** | Base Django Queryset | `Plan.objects.all()` |
| **`serializer_class`** | Default/Detail Serializer class | `PlanSerializer` |
| **`list_serializer_class`** | Serializer to use for listing action | `PlanListSerializer` |
| **`dropdown_serializer_class`** | Serializer for dropdown/picker queries | `PlanDropdownSerializer` |
| **`filterset_fields`** | List of lookup columns for filters | `["is_active", "plan_type"]` |
| **`search_fields`** | Match keys for text queries (`?search=...`) | `["name", "^code"]` |
| **`ordering_fields`** | Allowed fields for sorting parameters | `["sort_order", "created_at"]` |
| **`ordering`** | Default fallback sort behavior | `["sort_order", "-created_at"]` |
| **`select_related_fields`** | ForeignKey associations to join eagerly | `["tenant"]` |
| **`prefetch_related_fields`** | Many-to-Many / Reverse FK fields to prefetch | `["texts", "files"]` |

---

## 🧪 5. Database Migrations & Testing

* **Clean Migrations**: Always run `makemigrations <app_name>` followed by `migrate` to apply and test changes locally.
* **Isolate Integrity Checks**: In unit tests, wrap any operations that expect to raise database exceptions (like `IntegrityError` from unique-together constraints) in a `django.db.transaction.atomic()` block. This prevents marking the test transaction as broken and avoids subsequent `TransactionManagementError` failures.

---

## 📝 6. API Documentation Standards

Whenever developing or updating any REST API (whether using an `APIView` or `ModelViewSet`), you **MUST** create or update a corresponding documentation markdown (`.md`) file:

### 📁 Directory & File Structure
API documentation must be organized hierarchically at the root level of the project inside the `apiDocs/` directory, grouped by application:
```text
apiDocs/
└── <app_name>/
    └── <api_name>.md       # e.g., apiDocs/core/knowledge_base.md
```

### 📋 Required Documentation Coverage
Each `.md` document must cover all HTTP methods exposed by the API and explicitly detail:
1. **Endpoint Paths & HTTP Methods**: GET, POST, PUT, PATCH, DELETE.
2. **Request Payloads**: Full JSON body examples, URL query parameters, field types, and constraints.
   * **Explicit Field Validations**: You **MUST** identify exactly which fields have active runtime validations, including a clear description of the validation requirements for each validated field (e.g. maximum file sizes, required formats, domain constraints, unique conditions, etc.).
3. **Response Types & Status Codes**: Exact JSON response payloads for successful runs (e.g., HTTP 200, 201).
4. **Edge Cases & Error Handling**: Exact JSON responses and HTTP status codes for failures (e.g., HTTP 400 Bad Request, HTTP 401 Unauthorized, HTTP 403 Permission Denied, HTTP 404 Not Found, etc.).
5. **Search & Filtering Capabilities**:
   * Specify all search parameters (e.g., `?search=<term>`) and matching keys.
   * Document all available query parameter filtering keys (e.g., `?is_active=true` or `?processing_status=PENDING`).
   * Explicitly list **every possible key and all allowed/possible values** for those keys.
