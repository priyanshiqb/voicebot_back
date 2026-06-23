# Knowledge Base Files Cleanup Command

**Command Name:** `cleanup_orphaned_kb_files`
**Path:** `core/management/commands/cleanup_orphaned_kb_files.py`

## Purpose
A backend garbage-collection script designed to automatically clean up orphaned uploaded `KBFiles`. 

Orphaned files occur when a user uploads a file via the frontend for a Knowledge Base (which creates a database record and physically uploads the file to S3/MinIO), but then cancels the wizard or never associates the file with a specific `KnowledgeBase` record.

## Mechanics
This command systematically searches the database for `KBFiles` that:
1. Belong to a tenant (`tenant__isnull=False`).
2. Are **not** linked to any `KnowledgeBase` (`knowledge_bases__isnull=True`).
3. Are older than a specified time window. This safety window prevents accidentally deleting files that a user is actively in the process of uploading on the frontend.

When an orphaned record is processed for deletion, the custom `delete()` method on the `KBFiles` model is automatically invoked, ensuring that the physical object is also securely purged from the underlying S3/MinIO bucket.

## Usage

### 1. Basic Execution (Default)
By default, the command runs and deletes orphaned files that are older than **24 hours**.

```bash
uv run python manage.py cleanup_orphaned_kb_files
```

*(If running in Docker: `docker compose exec backend-dev python manage.py cleanup_orphaned_kb_files`)*

### 2. Dry Run Mode
To safely inspect how many files are currently orphaned and would be deleted without actually committing any changes, use the `--dry-run` flag.

```bash
uv run python manage.py cleanup_orphaned_kb_files --dry-run
```

### 3. Override Safety Timer
If you want to be more aggressive (or lenient) with the cleanup, you can override the default 24-hour safety timer using the `--older-than-hours` argument.

For example, to clean up files orphaned for just 1 hour:
```bash
uv run python manage.py cleanup_orphaned_kb_files --older-than-hours 1
```

### 4. Delete ALL Orphaned Files Immediately
If you want to force the script to delete absolutely ALL orphaned files right up to this exact second (bypassing the safety net entirely), you can set the timer to `0`:
```bash
uv run python manage.py cleanup_orphaned_kb_files --older-than-hours 0
```

### 5. Combining Arguments (Advanced Dry Run)
Because the flags are parsed independently, you can easily combine them to test custom timeframes before executing a real deletion. 

For example, to see all files orphaned over the last 5 hours without deleting them:
```bash
uv run python manage.py cleanup_orphaned_kb_files --dry-run --older-than-hours 5
```

## Scheduling
To ensure continuous repository hygiene, it is highly recommended to schedule this command to run automatically via a `cron` job or a Celery Beat task (e.g. running once per day at midnight).
