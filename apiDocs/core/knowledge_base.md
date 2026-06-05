# Knowledge Base REST API Specification

This document details the production REST API endpoints exposed by **`KnowledgeBaseViewSet`** under the resource path **`/api/v1/core/knowledge-base/`**.

---

## 🔒 Security & Multi-Tenancy Scoping

All endpoints require active bearer token authentication and automatically enforce row-level security (RLS):
* **Tenant Isolation**: Users can only see and manage records belonging to their active tenant mapping (`user.tenant_mapping.tenant`).
* **Metadata Protection**: Operational fields such as `tenant`, user audit tracking (`created_by`, `updated_by`, etc.), and background ETL statuses are locked as **read-only** and cannot be spoofed on write.

---

## 📈 Search, Filtering, and Pagination

### 🔍 Search Queries
* **Parameter**: `?search=<term>`
* **Matching Fields**: Contains (case-insensitive) match on `name`.

### 🎛️ Filter Parameters
* **`?is_processed=true|false`**
* **`?processing_status=PENDING|SUCCESS|FAILED`**

### ↕️ Sorting Options
* **Parameter**: `?ordering=<field>`
* **Allowed Sorting Keys**: `created_at`, `updated_at`, `name` (Prefix with `-` for descending order).
* **Default Order**: `[-created_at]`

### 📑 Paginated Schema
GET endpoints return a standard wrapped pagination envelope matching the following structure:
```json
{
  "status": true,
  "message": "success",
  "data": [...],
  "errors": null,
  "meta": {
    "count": 12,
    "next": "http://localhost:8000/api/v1/core/knowledge-base/?page=2",
    "previous": null,
    "page": 1,
    "page_size": 10
  }
}
```

---

## 📋 Endpoint Specifications

### 1. List Knowledge Bases
* **Path**: `GET /api/v1/core/knowledge-base/`
* **Response (HTTP 200 OK)**:
```json
{
  "status": true,
  "message": "Listed successfully",
  "data": [
    {
      "id": "e4b2d561-71fb-48eb-bf2e-0a5682855cf8",
      "name": "Help Center",
      "is_processed": false,
      "processing_status": "PENDING",
      "created_at": "2026-06-02T15:03:00.000Z"
    }
  ],
  "errors": null
}
```

### 2. Upload Document File
* **Path**: `POST /api/v1/core/knowledge-base/upload_file/`
* **Request Header**: `Content-Type: multipart/form-data`
* **Request Payload (Form-Data)**:
  * `file` (binary, **Required**): The document file to upload (`.pdf`, `.docx`, or `.txt`, Max size 10 MB).
* **Response (HTTP 201 Created)**:
```json
{
  "status": true,
  "message": "File uploaded successfully",
  "data": {
    "id": "uuid-preuploaded-file-id",
    "file": "http://localhost:8000/media/t-1/kb_files/setup.pdf",
    "original_name": "setup.pdf",
    "is_processed": false,
    "processing_status": "PENDING"
  }
}
```

---

### 3. Create Knowledge Base
* **Path**: `POST /api/v1/core/knowledge-base/`
* **Request Payload**:
```json
{
  "name": "Acme Guides",
  "texts": [
    {
      "title": "API Setup Guide",
      "description": "Guides to configure REST endpoints"
    }
  ],
  "files": [],
  "websites": [
    {
      "url": "https://acme-setup-guides.org"
    }
  ],
  "question_answers": [
    {
      "answer": "Yes, we support deep nesting.",
      "questions": [
        {
          "question": "Does this support nested creation?"
        }
      ]
    }
  ]
}
```
* **Runtime Validations & Schema Specifications**:
  * **Parent level**:
    * `name`: Required, max 255 chars. Must be unique within the tenant namespace. If a duplicate exists, returns HTTP 400 with `{"name": "A Knowledge Base with this name already exists."}`.
  
  * **Nested Sub-Component Specifications**:
    All 4 nested Many-to-Many fields accept arrays of JSON objects. Below are all possible fields, constraints, and validation rules for each type:

    | Category / Field | JSON Key | Type | Required | Constraints | Description & Validation Rules |
    | :--- | :--- | :--- | :--- | :--- | :--- |
    | **Texts (`texts`)** | | | | | |
    | * Title | `title` | string | **Yes** | Max 255 chars | The main heading/title for this text block. |
    | * Description | `description` | string | No | - | The detailed text content or guidelines body. |
    | **Files (`files`)** | | | | | |
    | * File | `file` | file / binary | **Yes** | Max 10 MB | Path or binary stream. Restrained strictly to `.pdf`, `.docx`, or `.txt`. |
    | **Websites (`websites`)** | | | | | |
    | * Website URL | `url` | string | **Yes** | Valid URL format | Domain crawling target. Must be unique within the active tenant to prevent duplicates. |
    | **Question Answers (`question_answers`)** | | | | | |
    | * Answer Block | `answer` | string | **Yes** | - | The primary answer to return for associated queries. |
    | * Questions List | `questions` | array | No | - | List of questions matching this answer. |
    | * Question String | `questions[].question` | string | **Yes** | - | Individual variation phrasing of the question. |

* **Response (HTTP 201 Created)**:
```json
{
  "status": true,
  "message": "Created successfully",
  "data": {
    "id": "8dc71f83-bf9c-4e89-bdc9-cd369a836da2",
    "tenant": "uuid-tenant-id",
    "name": "Acme Guides",
    "texts": [
      {
        "id": "uuid-text-1",
        "title": "API Setup Guide"
      }
    ],
    "files": [],
    "websites": [
      {
        "id": "uuid-web-1",
        "url": "https://acme-setup-guides.org"
      }
    ],
    "question_answers": [
      {
        "id": "uuid-qa-1",
        "answer": "Yes, we support deep nesting."
      }
    ],
    "is_processed": false,
    "processing_status": "PENDING",
    "processing_error": null,
    "last_processed_at": null,
    "processing_history": [],
    "created_by": "uuid-user-id",
    "created_at": "2026-06-02T15:04:12.000Z"
  },
  "errors": null
}
```

---

### 4. Retrieve Knowledge Base Details
* **Path**: `GET /api/v1/core/knowledge-base/<uuid>/`
* **Response (HTTP 200 OK)**: Returns the rich, prefetched nested relational representations of all subcomponents:
```json
{
  "status": true,
  "message": "Retrieved successfully",
  "data": {
    "id": "8dc71f83-bf9c-4e89-bdc9-cd369a836da2",
    "tenant": "uuid-tenant-id",
    "name": "Acme Guides",
    "texts": [
      {
        "id": "uuid-text-1",
        "title": "API Setup Guide"
      }
    ],
    "files": [
      {
        "id": "uuid-file-1",
        "original_name": "manual.pdf"
      }
    ],
    "websites": [
      {
        "id": "uuid-web-1",
        "url": "https://acme-setup-guides.org"
      }
    ],
    "question_answers": [
      {
        "id": "uuid-qa-1",
        "answer": "Yes, we support deep nesting."
      }
    ],
    "is_processed": false,
    "processing_status": "PENDING",
    "processing_error": null,
    "last_processed_at": null,
    "processing_history": []
  },
  "errors": null
}
```

---

### 5. Update Knowledge Base (Full / Partial)
* **Path**: `PUT /PATCH /api/v1/core/knowledge-base/<uuid>/`
* **Request Payload (Explicit Flag Operations - Create, Update, Delete)**:
To perform nested actions, pass the sub-objects with an explicit `flag` field:
1. **`create`**: Instantiates a brand new sub-object with the required fields and links it.
   * Format: `{"flag": "create", "title": "New Item", ...}`
2. **`update`**: Updates the existing record in-place by its `id` with any modified attributes.
   * Format: `{"id": "uuid-1", "flag": "update", "description": "Updated value"}`
3. **`delete`**: Safely soft-deletes the record by its `id` and unlinks it.
   * Format: `{"id": "uuid-2", "flag": "delete"}`

```json
{
  "name": "Acme Updated Guides",
  "texts": [
    {
      "id": "uuid-text-1",
      "flag": "update",
      "description": "Updated JSON descriptions"
    },
    {
      "flag": "create",
      "title": "A New Text Component",
      "description": "Newly created text"
    },
    {
      "id": "uuid-text-2",
      "flag": "delete"
    }
  ]
}
```
* **Response (HTTP 200 OK)**:
```json
{
  "status": true,
  "message": "Updated successfully",
  "data": {
    "id": "8dc71f83-bf9c-4e89-bdc9-cd369a836da2",
    "name": "Acme Updated Guides",
    "texts": [
      {
        "id": "uuid-text-1",
        "title": "Setup Guide"
      }
    ]
  },
  "errors": null
}
```

---

### 6. Soft-Delete Knowledge Base
* **Path**: `DELETE /api/v1/core/knowledge-base/<uuid>/`
* **Operational Hook**: Safely triggers recursive soft-deletion cascading through all Many-to-Many models inside an atomic transaction, marking them `is_deleted=True` and suffixing namespaces.
* **Response (HTTP 200 OK)**:
```json
{
  "status": true,
  "message": "Deleted successfully",
  "data": null,
  "errors": null
}
```

---

### 7. Dropdown Picker Option List
* **Path**: `GET /api/v1/core/knowledge-base/dropdown/`
* **Response (HTTP 200 OK)**:
```json
{
  "status": true,
  "message": "Dropdown options retrieved successfully",
  "data": [
    {
      "label": "Acme Guides",
      "value": "8dc71f83-bf9c-4e89-bdc9-cd369a836da2"
    }
  ],
  "errors": null
}
```

---

### 8. Bulk Delete Action
* **Path**: `POST /api/v1/core/knowledge-base/bulk_delete/`
* **Request Payload**:
```json
{
  "ids": [
    "uuid-id-1",
    "uuid-id-2"
  ]
}
```
* **Response (HTTP 200 OK)**:
```json
{
  "status": true,
  "message": "Bulk deleted successfully",
  "data": null,
  "errors": null
}
```

---

### 9. Check Knowledge Base Name Uniqueness
* **Path**: `POST /api/v1/core/knowledge-base/check-name/`
* **Authentication**: Bearer JWT (IsAuthenticated)
* **Request Payload**:
```json
{
  "name": "Acme Guides",
  "exclude_id": "optional-uuid-to-exclude"
}
```
* **Response (HTTP 200 OK)**:
```json
{
  "status": true,
  "message": "Success",
  "data": {
    "is_unique": true
  },
  "errors": null
}
```

---

### 10. Check Website URL Uniqueness
* **Path**: `POST /api/v1/core/knowledge-base/check-url/`
* **Authentication**: Bearer JWT (IsAuthenticated)
* **Request Payload**:
```json
{
  "url": "https://acme-setup-guides.org",
  "exclude_id": "optional-uuid-to-exclude"
}
```
* **Response (HTTP 200 OK)**:
```json
{
  "status": true,
  "message": "Success",
  "data": {
    "is_unique": false
  },
  "errors": null
}
```

