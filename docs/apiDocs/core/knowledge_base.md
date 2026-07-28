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
* **`?processing_status=PENDING|PROCESSING|COMPLETED|FAILED`**

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
  * `extension` (string, **Optional**): The extension of the file (e.g. `".pdf"`, `".docx"`, `".txt"`, `"pdf"`, `"docx"`, or `"txt"`). If not supplied, it is automatically extracted from the uploaded file's name.
* **Response (HTTP 201 Created)**:
```json
{
  "status": true,
  "message": "File uploaded successfully",
  "data": {
    "id": "uuid-preuploaded-file-id",
    "file": "http://localhost:8000/media/t-1/kb_files/setup.pdf",
    "original_name": "setup.pdf",
    "extension": ".pdf",
    "is_processed": false,
    "processing_status": "PENDING"
  }
}
```

---

### 2.2 [NEW] Direct S3 File Upload (Presigned PUT) - Step 1: Request URL
* **Path**: `POST /api/v1/core/knowledge-base/request-upload-url/`
* **Request Payload**:
```json
{
  "filename": "document.pdf",
  "content_type": "application/pdf",
  "file_size_in_bytes": 102400
}
```
* **Description**: The modern way to upload files. Generates a secure URL allowing the frontend to upload a file directly to the S3/MinIO bucket.
* **Response (HTTP 200 OK)**:
```json
{
  "status": true,
  "message": "Upload URL generated successfully.",
  "data": {
    "upload_url": "http://localhost:9000/t-1/...pdf?X-Amz-Signature=...",
    "object_key": "t-1/kb_files/uuid-document.pdf"
  }
}
```

---

### 2.3 [NEW] Direct S3 File Upload (Presigned PUT) - Step 2: Confirm Upload
* **Path**: `POST /api/v1/core/knowledge-base/confirm-upload/`
* **Request Payload**:
```json
{
  "object_key": "t-1/kb_files/uuid-document.pdf",
  "original_name": "document.pdf",
  "extension": ".pdf"
}
```
* **Description**: To be called *after* the frontend successfully `PUT`s the file to the `upload_url`. The backend will verify the file physically exists in the S3 bucket, and then create the standard `KBFiles` database record.
* **Response (HTTP 201 Created)**:
*(Identical to the legacy upload_file response)*
```json
{
  "status": true,
  "message": "File confirmed and saved successfully.",
  "data": {
    "id": "uuid-preuploaded-file-id",
    "file": "http://localhost:8000/media/t-1/kb_files/uuid-document.pdf",
    "original_name": "document.pdf",
    "extension": ".pdf",
    "is_processed": false,
    "processing_status": "PENDING"
  }
}
```

---

### 2.1. Discard Uploaded Document Files
* **Path**: `POST /api/v1/core/knowledge-base/discard-files/`
* **Request Payload**:
```json
{
  "file_ids": [
    "uuid-file-id-1",
    "uuid-file-id-2"
  ]
}
```
* **Description**: Safely hard-deletes an array of uploaded files that are no longer needed (e.g., when a user uploads files but then cancels/discards the Knowledge Base creation wizard). This deletes both the database records and the underlying S3 storage objects in a single batch operation.
* **Response (HTTP 200 OK)**:
```json
{
  "status": true,
  "message": "2 file(s) discarded successfully.",
  "data": null,
  "errors": null
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
    | * Pre-uploaded File ID | `id` | uuid | Cond. | - | **Primary flow:** link a file already uploaded via `upload_file/` or `confirm-upload/` by its `id`. Provide either this `id` **or** an inline `file` binary. |
    | * File | `file` | file / binary | Cond. | Max 10 MB | Inline upload alternative. Path or binary stream, restrained strictly to `.pdf`, `.docx`, or `.txt`. |
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
    "tenant": {
      "id": "uuid-tenant-id",
      "name": "Alpha Corp"
    },
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
    "etl_id": null,
    "pending_at": null,
    "processing_at": null,
    "completed_at": null,
    "failed_at": null,
    "created_by": {
      "id": "uuid-user-id",
      "name": "Emma"
    },
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
    "tenant": {
      "id": "uuid-tenant-id",
      "name": "Alpha Corp"
    },
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
        "original_name": "manual.pdf",
        "extension": ".pdf"
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
    "etl_id": null,
    "pending_at": null,
    "processing_at": null,
    "completed_at": null,
    "failed_at": null
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
* **Query Parameters**:
  * `force=true` (optional): Required to bypass validation if the knowledge base is connected to an active agent.
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
* **Response (HTTP 400 Bad Request - Connected to Agent Warning)**:
Returned if the Knowledge Base is connected to an agent and `force=true` was not passed.
```json
{
  "status": false,
  "warning": true,
  "message": "This knowledge base is connected to the following agents: SalesBot... Are you sure you want to delete it?",
  "errors": {
    "detail": "This knowledge base is currently in use by agents: SalesBot... Please confirm to force delete."
  }
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
* **Path**: `POST /api/v1/core/knowledge-base/bulk-delete/`
* **Query Parameters**:
  * `force=true` (optional): Required to bypass validation if any selected knowledge base is connected to an active agent.
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
* **Response (HTTP 400 Bad Request - Connected to Agent Warning)**:
Returned if any selected Knowledge Base is connected to an agent and `force=true` was not passed.
```json
{
  "status": false,
  "warning": true,
  "message": "One or more selected knowledge bases are connected to an agent. Are you sure you want to delete them?",
  "errors": {
    "detail": "Connected to agent. Pass force=true to proceed."
  }
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

---

### 11. Update ETL status of a Knowledge Base component
* **Path**: `POST /api/v1/core/knowledge-base/<kb_uuid>/update-etl-status/`
* **Authentication**: Static Bearer Token (`ETLSystemTokenAuthentication`) via header: `Authorization: Bearer <ETL_SYSTEM_TOKEN>`
* **Permissions**: Restricted to users with type `ETL` (`IsETLUser`). This system user has global cross-tenant access to bypass typical multi-tenant isolation constraints.
* **Request Payload**:
```json
{
  "type": "text",
  "object_id": "uuid-of-the-object",
  "etl_id": "uuid-of-the-etl-task",
  "status": "COMPLETED",
  "timestamp": "optional-iso-datetime",
  "error_message": "optional-error-message-if-failed"
}
```
  * **Explicit Field Validations**:
    * `type`: Choice field. Allowed values: `knowledgebase`, `file`, `website`, `text`, `qa`.
    * `object_id`: UUID. Must match the specified Knowledge Base component type and belong to that Knowledge Base.
    * `etl_id`: UUID. Must match the current tracking `etl_id` on the object if one is already set.
    * `status`: Choice field. Allowed values: `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`.
* **Response (HTTP 200 OK - Success)**:
```json
{
  "status": true,
  "message": "ETL status updated successfully."
}
```
* **Response (HTTP 400 Bad Request - e.g. Mismatched etl_id)**:
```json
{
  "status": false,
  "message": "Failed to update ETL status. The etl_id does not match the current tracking etl_id."
}
```

---

### 12. Outbound External Ingestion (ETL) API Trigger
When a Knowledge Base is created or updated, the backend automatically dispatches a single HTTP POST request to the external ETL server (configured via `ETL_SERVER_URL`).

* **HTTP Method**: `POST`
* **Trigger Payload Example**:
```json
{
  "etl_id": "36c7c81e-f85f-4de7-85eb-aba467037fa7",
  "callback_update_url": "http://localhost:8000/api/v1/core/knowledge-base/20ab4a80-bbf8-4c9e-9c42-79138832db8a/update-etl-status/",
  "data": {
    "id": "20ab4a80-bbf8-4c9e-9c42-79138832db8a",
    "name": "Acme Ingestion",
    "is_processed": false,
    "processing_status": "PENDING",
    "is_deleted": false,
    "callback_update_url": "http://localhost:8000/api/v1/core/knowledge-base/20ab4a80-bbf8-4c9e-9c42-79138832db8a/update-etl-status/",
    "texts": [
      {
        "id": "78c8c81e-f85f-4de7-85eb-aba467037fa8",
        "title": "Setup",
        "description": "Guides to configure REST endpoints",
        "is_processed": false,
        "processing_status": "PENDING",
        "etl_id": "36c7c81e-f85f-4de7-85eb-aba467037fa7",
        "is_deleted": false
      }
    ],
    "files": [
      {
        "id": "18c8c81e-f85f-4de7-85eb-aba467037fa9",
        "file": "http://localhost:8000/media/t-1/kb_files/setup.pdf",
        "original_name": "setup.pdf",
        "extension": ".pdf",
        "file_size_in_bytes": 102400,
        "is_processed": false,
        "processing_status": "PENDING",
        "etl_id": "36c7c81e-f85f-4de7-85eb-aba467037fa7",
        "is_deleted": false
      }
    ],
    "websites": [
      {
        "id": "28c8c81e-f85f-4de7-85eb-aba467037fb0",
        "url": "https://acme-setup-guides.org",
        "is_processed": false,
        "processing_status": "PENDING",
        "etl_id": "36c7c81e-f85f-4de7-85eb-aba467037fa7",
        "is_deleted": false
      }
    ],
    "question_answers": [
      {
        "id": "38c8c81e-f85f-4de7-85eb-aba467037fb1",
        "answer": "Yes, we support deep nesting.",
        "is_processed": false,
        "processing_status": "PENDING",
        "etl_id": "36c7c81e-f85f-4de7-85eb-aba467037fa7",
        "is_deleted": false,
        "questions": [
          {
            "id": "48c8c81e-f85f-4de7-85eb-aba467037fb2",
            "question": "Does this support nested creation?"
          }
        ]
      }
    ]
  }
}
```
* **Processing Logic**: The external ETL server receives this payload and processes only items where `etl_id` matches the request `etl_id` and `processing_status` is `PENDING`. It will skip already processed items (where `is_processed` is `true` or `etl_id` is different) and delete items where `is_deleted` is `true`.


