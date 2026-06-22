# S3 Storage Service Architecture & Request Profiling

The `S3StorageService` handles interactions with S3-compatible object storage backends (MinIO for local development and Cloudflare R2 for production environments).

---

## Configuration Variables

The service dynamically configures itself via the following variables in `settings.py`:

* `S3_ENDPOINT_URL`: Endpoint of the S3-compatible API (e.g. `http://localhost:9000` locally).
* `S3_ACCESS_KEY_ID`: S3 credentials identifier.
* `S3_SECRET_ACCESS_KEY`: S3 credentials secret key.
* `S3_BUCKET_NAME`: The single global bucket where all client uploads reside.
* `S3_REGION_NAME`: Target region (defaults to `us-east-1`).
* `S3_CUSTOM_DOMAIN`: Optional domain alias for serving public URLs directly.

---

## API Request Profiling (Class A & Class B Operations)

To ensure cost predictability under production load (especially when using transaction-based services like Cloudflare R2 or Amazon S3), standard API calls are categorized into **Class A** (creation, listing, update) and **Class B** (retrieval, checking) operations.

| Function Name | Description | Boto3 SDK APIs Invoked | Class A Operations | Class B Operations | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`__init__`** | Initializes the S3 client | None | **0** | **0** | Purely local execution; does not contact the storage server. |
| **`create_bucket_if_not_exists`** | Assures target bucket availability | `head_bucket`<br>`create_bucket` (conditional) | **0** (bucket exists)<br>**1** (creates bucket) | **1** | First executes a `HEAD` request to verify existence, then `PUT` bucket if 404 is returned. |
| **`get_presigned_url_for_object`** | Secure access link for a single file | None (`generate_presigned_url`) | **0** | **0** | Signature generated client-side locally. Zero network cost. |
| **`get_presigned_url_for_folder`** | Scoped access token for a prefix | None (`generate_presigned_url`) | **0** | **0** | Signature generated client-side locally. Zero network cost. |
| **`get_folder_disk_usage`** | Calculates folder footprint and file count | `list_objects_v2` (paginated) | **N** (1 per 1,000 files) | **0** | Uses `ListObjectsV2`. A directory containing ≤ 1,000 objects takes exactly **1 Class A** operation. |
| **`get_file_disk_usage`** | Fetches file size and mimetype | `head_object` | **0** | **1** | Uses a standard metadata lookup (`HEAD`). |
| **`delete_blob_direct`** | Permanently deletes an object | `delete_object` | **0** | **0** | Delete operations are **free of charge** in Cloudflare R2 and AWS S3. |

---

## Summary Tips for Cost Optimization

* **Presigned URLs**: Generating presigned URLs (`get_presigned_url_for_object` / `get_presigned_url_for_folder`) is calculated offline using cryptography. You can generate these access URLs frequently in your APIs or serializer lookups without making S3 calls or incurring request fees.
* **Bucket Check Mitigation**: In production, the target storage bucket is pre-provisioned. You can disable the runtime `create_bucket_if_not_exists` safety checks to eliminate redundant `head_bucket` calls.
