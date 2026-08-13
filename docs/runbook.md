# Command Runbook

Every command you need to run this project — setup, the stack, tests, migrations,
maintenance, and the failures that look like bugs but are not.

> **`uv run python` or plain `python`?** Both work. `uv run python manage.py …` resolves the
> project's locked dependencies without an activated virtualenv; plain `python manage.py …`
> is fine once your venv is active. Every command below is written with `uv run`; drop it if
> your venv is already active.

---

## 0. Which mode are you in?

This decides almost every command below. Pick one and stay in it.

| | **All in Docker** | **Infra in Docker, Django on the host** |
|---|---|---|
| Django runs | in `backend-dev` | on your machine |
| Postgres/Redis reached at | `pgbouncer:5432`, `redis:6379` | `127.0.0.1:5579`, `127.0.0.1:5563` |
| Needs `.env.local` | No | **Yes** |
| Run manage.py with | `docker compose exec backend-dev python manage.py …` | `uv run python manage.py …` |
| Best for | matching production wiring | debugging, fast iteration, running tests |

**The host mode exists because `.env` is written for the Docker network** — it names
services (`pgbouncer`, `redis`) that do not resolve from your machine. `.env.local` re-points
exactly those keys at published ports and nothing else. It is read **first** and wins.

---

## 1. First-time setup

```bash
cp .env.example .env                 # then fill in the secrets
cp .env.local.example .env.local     # only for host mode
uv sync                              # install locked dependencies
```

Read the annotations in [`.env.example`](../.env.example) rather than guessing — every key
records its default, its accepted values, and what breaks if it is wrong.

---

## 2. The Docker stack

Services are grouped by **profile**; nothing starts without one.

| Profile | Services | You need it for |
|---|---|---|
| `database` | `postgres` | Everything |
| `caches` | `redis` | Everything — cache, Celery broker, channels, **login throttling** |
| `pgbouncer` | `pgbouncer` | Container-mode Django (`.env` points at it) |
| `dev` | `backend-dev`, `celery-dev`, `celery-beat-dev` | Full-Docker mode |
| `prod` | `backend-wsgi`, `backend-asgi`, `celery-prod`, `celery-beat-prod` | Production-shaped run |
| `emulators` | `minio` | Local S3 instead of the shared R2 bucket |
| `gateway` | `caddy` | TLS / routing in front |
| `pgadmin`, `redis-insight` | admin UIs | Poking at data |
| `grafana`, `logs-monitoring`, `matrics-monitoring`, `pgexporter`, `redis-exporter` | observability | Dashboards |
| `backend-docs` | `backend-docs` | Serving this documentation |

```bash
# Minimum for host-mode development
docker compose --profile database --profile caches up -d

# Add local object storage (create the bucket once at http://127.0.0.1:9001)
docker compose --profile emulators up -d

# Everything in Docker
docker compose --profile database --profile caches --profile pgbouncer --profile dev up -d

# Status, logs, teardown
docker compose ps
docker compose logs -f backend-dev
docker compose logs -f celery-dev
docker compose down                 # keeps volumes
docker compose down -v              # DESTROYS database and redis data
```

---

## 3. Running the app

```bash
# Host mode
uv run python manage.py runserver                    # WSGI, no websockets
uv run daphne -b 0.0.0.0 -p 8000 config.asgi:application   # ASGI, websockets work

# Celery on the host (needs redis up)
uv run celery -A config worker --loglevel=info
uv run celery -A config beat --loglevel=info

# Docker mode — already running under the `dev` profile
docker compose exec backend-dev python manage.py shell
```

> **`runserver` cannot serve WebSockets.** Use `daphne` if you are working on anything under
> [WS Event Protocol](wsDocs/instruction.md).

---

## 4. Database & migrations

```bash
uv run python manage.py makemigrations
uv run python manage.py makemigrations <app> --name <descriptive_name>
uv run python manage.py migrate
uv run python manage.py showmigrations core

# Fail if a model change has no migration — worth running before you commit
uv run python manage.py makemigrations --check --dry-run

uv run python manage.py createsuperuser
uv run python manage.py dbshell
```

---

## 5. Tests

### The one flag that matters

```bash
uv run python manage.py test --settings=config.settings.test_postgres --noinput
```

**Always pass `--settings=config.settings.test_postgres`.** The default settings force
in-memory SQLite, which cannot host features this project depends on — the `ArrayField` on
`AgentCallLog`, and the `ExclusionConstraint` that is the authoritative guarantee against
double-booking. Without the flag you get failures that have nothing to do with your change.

`--noinput` answers the "delete the stale test database?" prompt automatically. Without it, a
run that was interrupted earlier leaves its `test_<db>` behind and the next run dies with
`EOFError: EOF when reading a line`.

### What the test settings change

| | Effect |
|---|---|
| Database | real Postgres, throwaway `test_<POSTGRES_DB>` — your dev database is never touched |
| Cache & channels | in-memory, not Redis |
| Email | in-memory outbox, including tenant SMTP — so tests can assert on messages |
| Celery | `ALWAYS_EAGER` — tasks run inline, and exceptions propagate |
| Passwords | MD5 hashing, purely for speed |
| Storage | a temp directory, not S3 |

### Host mode needs the connection overrides

`.env.local` supplies these for a normal run. If you are invoking tests in an environment
that does not read it, pass them inline:

```bash
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5579 \
REDIS_HOST=127.0.0.1 REDIS_PORT=5563 \
uv run python manage.py test --settings=config.settings.test_postgres --noinput
```

### Scoping a run

```bash
# Everything
uv run python manage.py test --settings=config.settings.test_postgres --noinput

# One app
uv run python manage.py test core --settings=config.settings.test_postgres --noinput
uv run python manage.py test user tenant onboard --settings=config.settings.test_postgres --noinput

# One module, class, or single test
uv run python manage.py test core.tests.test_device_token_lifecycle --settings=config.settings.test_postgres --noinput
uv run python manage.py test core.tests.test_device_token_lifecycle.SessionLinkTests --settings=config.settings.test_postgres --noinput
uv run python manage.py test core.tests.test_device_token_lifecycle.SessionLinkTests.test_registration_links_the_device_to_the_current_session --settings=config.settings.test_postgres --noinput
```

### Useful flags

| Flag | Effect | Caution |
|---|---|---|
| `--noinput` | Never prompt; reuse/destroy the test DB silently | Use always |
| `--keepdb` | Skip create/destroy between runs — much faster iteration | **Stale after a migration.** Drop it when schema changes |
| `--failfast` | Stop at the first failure | |
| `-v 2` | Print each test name as it runs | |
| `--parallel` | Split across processes | Not verified against this suite; some tests share module-level state |

### Reading the result correctly

```bash
uv run python manage.py test --settings=config.settings.test_postgres --noinput > /tmp/out.txt 2>&1
echo "EXIT=$?"
grep -E "^(Ran |OK|FAILED)" /tmp/out.txt
```

> **Do not pipe the test command into `tail`/`grep` and read `$?`** — that is the exit status
> of the *last* command in the pipe, not the test runner's. A failing suite will look like it
> passed. Write to a file and check `$?` immediately, as above.

### What must be running

| Test area | Needs |
|---|---|
| Everything | **Postgres** |
| `defender` / login-lockout tests | **Redis** — django-defender uses `DEFENDER_REDIS_URL` and ignores the in-memory `CACHES` the test settings set. If Redis is down you get ~14 unrelated-looking failures |
| Contact-import and upload tests | Nothing — S3 calls fail and are tolerated. The `Could not connect to the endpoint URL: http://127.0.0.1:9000` lines are **expected noise** unless you started the `emulators` profile |
| Knowledge-base tests | Nothing — `Failed to contact external ETL server` is likewise expected noise |

---

## 6. Maintenance commands

Full detail in [Maintenance Commands](commands/README.md). Every destructive one supports
`--dry-run`; run that first.

### Scheduled — these already run on Celery Beat

You only run these by hand to investigate or to force an early pass.

```bash
uv run python manage.py cleanup_pending_uploads --dry-run     # abandoned presigned uploads
uv run python manage.py purge_expired_sessions --dry-run      # sessions + JWT blacklist + orphaned devices
uv run python manage.py purge_device_tokens --dry-run         # stale/dead FCM tokens
```

```bash
uv run python manage.py purge_device_tokens --verify          # ask FCM to confirm before retiring
uv run python manage.py purge_expired_sessions --older-than-days 90
```

### On demand

```bash
# Bucket objects no database row knows about. Read-only unless --delete.
uv run python manage.py audit_orphaned_objects --verbose
uv run python manage.py audit_orphaned_objects --tenant-id <uuid> --older-than-hours 48
uv run python manage.py audit_orphaned_objects --delete        # DESTRUCTIVE — read the doc first

# KB files never attached to a Knowledge Base
uv run python manage.py cleanup_orphaned_kb_files --dry-run --older-than-hours 24

# Rebuild the bookable-slot horizon
uv run python manage.py materialize_availability --dry-run
uv run python manage.py materialize_availability --tenant <id> --days 60 --purge-past

# Booking data integrity
uv run python manage.py audit_bookings --check --limit 100

# One-off backfill after the contact-link migration
uv run python manage.py backfill_contact_links --tenant-id <uuid> --batch-size 500
```

> **`audit_orphaned_objects` deletes from the *absence* of a row**, which is why it reports by
> default and a human decides. `cleanup_pending_uploads` deletes from the *presence* of one it
> can inspect, which is why that one is safe to schedule. They approach the same bucket from
> opposite ends and neither sees what the other sees.

### Security

```bash
uv run python manage.py audit_upload_key_ownership --verbose
```

> **A non-empty result here is a security incident, not a bug report** — it means one tenant's
> row points at another tenant's object. Preserve the output and follow disclosure before
> deleting anything.

### Data loading

```bash
uv run python manage.py load_data                              # seed users/roles
uv run python manage.py load_postal_data --csv <path> --force  # geography reference data
uv run python manage.py rebuild_bloom_filter                   # phone-availability filter
uv run python manage.py audit_timezone_offsets --year 2026     # report DST drift
uv run python manage.py audit_timezone_offsets --fix           # and correct it
```

---

## 7. API schema

```bash
# Generate the OpenAPI document
uv run python manage.py spectacular --file schema.yml

# Treat warnings as failure (stricter; note it aborts mid-write, so the file may be partial)
uv run python manage.py spectacular --fail-on-warn --file schema.yml
```

Served by the running app:

| Path | What |
|---|---|
| `/api/schema/` | The raw OpenAPI document |
| `/api/docs/` | Swagger UI |
| `/api/redoc/` | ReDoc |

> **Verify schema changes against the generated document, not the source.** A decorator that
> looks right can still fail to reach the output. The schema-regression tests in
> `core.tests.catalog.views.test_regressions` generate the whole document and assert it is
> error-free; run them after any `extend_schema` edit.

---

## 8. Checks to run before committing

```bash
uv run python manage.py check                                   # system checks
uv run python manage.py makemigrations --check --dry-run        # no un-migrated model changes
uv run python manage.py test --settings=config.settings.test_postgres --noinput
```

`manage.py check` currently reports one **expected** warning:

```
(core.W001) Presigned uploads are using PUT, which cannot enforce a size limit.
```

That is a deliberate, documented state — see [File Uploads](flows/file-upload.md). Any
*other* check output is a real finding.

---

## 9. When something looks broken but isn't

| Symptom | Cause | Fix |
|---|---|---|
| `EOFError: EOF when reading a line` on test start | A previous run was killed and left its test database | Add `--noinput` |
| ~14 failures around login/lockout | Redis is down; django-defender uses `DEFENDER_REDIS_URL`, independent of the test cache | Start the `caches` profile |
| `ArrayField`/constraint errors across many tests | Ran without `--settings=config.settings.test_postgres` | Add the flag |
| `Could not connect to … 127.0.0.1:9000` | MinIO not running | Expected noise, or start the `emulators` profile |
| `Failed to contact external ETL server` | The ETL service is not running | Expected noise |
| `could not translate host name "pgbouncer"` | Host mode without `.env.local` | Create it from `.env.local.example` |
| Login 500s, everything else fine | `REDIS_PORT` wrong in host mode — defender fails on the login path first | Set `REDIS_PORT=5563` |
| Uploads silently land in `./media` | An S3 key was set to an **empty** value, flipping `USE_S3_STORAGE` off | Omit the key entirely, or set all four together |
| Tests pass but production is broken | A mock returned a different shape than the real service | Pin the real contract in a test — see `core/tests/test_storage_service_contract.py` |
| A schema decorator "did nothing" | `--fail-on-warn` aborted before writing the file, so you read a stale one | Generate without the flag, then inspect |

---

## Related

- [Maintenance Commands](commands/README.md) — what each sweep deletes, and why
- [`.env.example`](../.env.example) — every setting, annotated with defaults
- [`.env.local.example`](../.env.local.example) — the host-mode overrides, and what not to copy
- [Authentication](flows/authentication.md) · [File Uploads](flows/file-upload.md) · [Push Notifications](flows/push-notifications.md)
