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
| `emulators` | `azurite` | Azure Blob emulator. **Not usable by the app yet** — it does not speak S3; see the migration plan |
| `gateway` | `caddy` | TLS / routing in front |
| `pgadmin`, `redis-insight` | admin UIs | Poking at data |
| `grafana`, `logs-monitoring`, `matrics-monitoring`, `pgexporter`, `redis-exporter` | observability | Dashboards |
| `backend-docs` | `backend-docs` | Serving this documentation |

```bash
# Minimum for host-mode development
docker compose --profile database --profile caches up -d

# Azure Blob emulator. Starts and works, but the app still speaks S3 and cannot
# use it until the storage layer is ported — keep using the shared R2 bucket.
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

## 3b. The documentation portal (this site)

Served by the `backend-docs` profile on `DOCS_PORT` (default **8088**), and protected by
**HTTP basic auth enforced inside the container** — not at the gateway, because the port is
published straight onto the host and never passes through Caddy's gateway routes.

```bash
docker compose --profile backend-docs up -d
# then http://127.0.0.1:8088  →  browser prompts for credentials
```

### Setting the credential

```bash
# 1. Generate a bcrypt hash
docker run --rm caddy:2.7-alpine caddy hash-password --plaintext 'your-password'
#    → $2a$14$z.VBN2Dcyz...

# 2. DOUBLE every "$" and put it in .env
#    DOCS_BASIC_AUTH_USER=docs
#    DOCS_BASIC_AUTH_PASSWORD_HASH=$$2a$$14$$z.VBN2Dcyz...

# 3. Recreate the container so it picks up the new config
docker compose --profile backend-docs up -d --force-recreate backend-docs
```

> **Doubling the `$` is mandatory, and it is the one thing people get wrong.** A bcrypt hash
> is full of `$`, and compose reads a single `$` as variable interpolation. Store the doubled
> form; compose collapses it back to single on the way into the container.
>
> Verify what actually arrived:
> ```bash
> docker compose --profile backend-docs run --rm --no-deps --entrypoint sh backend-docs \
>   -c 'echo $DOCS_BASIC_AUTH_PASSWORD_HASH'
> ```
> It should print **single** dollars — `$2b$12$…`. If it prints `$$2b$$12$$…`, the value in
> `.env` was not doubled.

Both keys default to `GATEWAY_BASIC_AUTH_*`, so an existing deployment keeps working without
any change. Set them explicitly if documentation readers should not also hold the
Prometheus / Loki / RedisInsight credential.

> ⚠️ **Publishing a port bypasses the gateway.** This applies to more than docs: `loki`,
> `prometheus`, `grafana`, `redis-insight` and `azurite` all publish host ports too, so the
> `basicauth` on their `*.{DOMAIN}` routes does **not** protect direct access to those ports.
> Keep `BIND_IP=127.0.0.1` unless you specifically intend LAN exposure.

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

### Data migrations run once, against rows you cannot see

`onboard.0025` and `wallet.0004` carry `RunPython` steps that edit live billing state.
They execute exactly once per environment and then never again, so a bug in one
corrupts data at the moment it runs with no second chance to notice.

`onboard/tests/test_migrations.py` rewinds the database, builds the "before" world with
**historical** models, migrates forward and asserts what changed — which is the only
way to exercise them, because building the test database from zero runs every step
against no rows at all.

> **`onboard.0025` had a real bug found this way.** Its duplicate-resolution step
> writes `superseded_by`, and Django creates foreign keys as `DEFERRABLE INITIALLY
> DEFERRED` on PostgreSQL — so the `AddConstraint` later in the same migration failed
> with *"cannot CREATE INDEX … because it has pending trigger events"*. It only fired
> when a tenant actually **had** duplicate active subscriptions, which no environment
> did when the migration was written. Fixed by flushing the deferred checks before the
> index is built.
>
> If you have already applied `0025` you are unaffected; the fix matters for
> environments that have not migrated yet.

Before adding a `RunPython` that changes rows in the same migration as a schema
operation on the same table, check whether the DML queues deferred triggers — or put
the schema change in its own migration.

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

### Two suites are opt-in, because they leave the machine

Everything else stubs the gateway and Firebase. These two do not, so they are skipped
unless you ask for them — CI, a plane, and a colleague without credentials all still
get a green run.

```bash
# Real Razorpay, in test mode. Creates orders that are never paid.
RUN_LIVE_GATEWAY_TESTS=1 uv run python manage.py test onboard.tests.test_razorpay_live \
    --settings=config.settings.test_postgres --noinput

# Real Firebase, using the configured FIREBASE_CREDENTIALS.
RUN_LIVE_PUSH_TESTS=1 uv run python manage.py test core.tests.billing.test_push_live \
    --settings=config.settings.test_postgres --noinput
```

**`RAZORPAY_WEBHOOK_SECRET` gates four of the twelve gateway cases** — the signature
ones. Without it they skip, and *nothing anywhere* exercises signature verification:
every other webhook test in the suite runs with `PAYMENTS_ALLOW_UNSIGNED=True`. Set it
if you are changing anything on that path.

Neither suite completes a payment or delivers to a device. Checkout needs a browser,
and FCM reports acceptance rather than delivery — both are manual QA.

### Some billing tests are slow, and have to be

`wallet/tests/test_concurrency.py` and `onboard/tests/test_migrations.py` use
`TransactionTestCase`, which truncates tables between tests and cannot use
`--keepdb` meaningfully. Roughly a minute and four minutes respectively.

The cost buys the only two things a normal `TestCase` cannot prove: that two
connections cannot spend the same credit (a `TestCase` rolls back its transaction, so
the race *cannot happen* there), and that the one-shot data migrations do what they
claim when they actually find rows.

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
| Contact-import and upload tests | Nothing — S3 calls fail and are tolerated. `Could not connect to the endpoint URL` lines are **expected noise**; there is no local S3 to start any more |
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
| `Could not connect to …` from a storage call | No local S3 exists since MinIO was removed | Expected noise in tests; point `.env` at R2 for real uploads |
| `azurite-voicebot` restart-loops with `Critical error happens during GC` | Stale Azurite state in `.data/azurite` from an earlier run — its database references extents that no longer exist | `docker compose stop azurite`, delete (or rename) `.data/azurite`, start again. The directory is disposable |
| `Failed to contact external ETL server` | The ETL service is not running | Expected noise |
| `could not translate host name "pgbouncer"` | Host mode without `.env.local` | Create it from `.env.local.example` |
| Login 500s, everything else fine | `REDIS_PORT` wrong in host mode — defender fails on the login path first | Set `REDIS_PORT=5563` |
| Uploads silently land in `./media` | An S3 key was set to an **empty** value, flipping `USE_S3_STORAGE` off | Omit the key entirely, or set all four together |
| Tests pass but production is broken | A mock returned a different shape than the real service | Pin the real contract in a test — see `core/tests/test_storage_service_contract.py` |
| A schema decorator "did nothing" | `--fail-on-warn` aborted before writing the file, so you read a stale one | Generate without the flag, then inspect |
| Docs portal stuck on **"Loading Developer Portal…"** | A `//cdn.jsdelivr.net` URL in `docs/index.html` resolves to `http://` over plain HTTP, which the CSP's `https:` source rejects — every script is blocked | Use explicit `https://` for all external assets in `index.html`; then hard-refresh (`Ctrl+Shift+R`) |
| Docs 401 with the right password | The bcrypt hash is not a hash of that password. **You cannot compare two bcrypt hashes** — they are salted | Regenerate with the password single-quoted, double the `$`, `--force-recreate` |

---

## Related

- [Maintenance Commands](commands/README.md) — what each sweep deletes, and why
- [`.env.example`](../.env.example) — every setting, annotated with defaults
- [`.env.local.example`](../.env.local.example) — the host-mode overrides, and what not to copy
- [Authentication](flows/authentication.md) · [File Uploads](flows/file-upload.md) · [Push Notifications](flows/push-notifications.md)
