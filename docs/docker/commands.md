# 🐳 Unified Developer Commands Cheatsheet

This cheatsheet provides all standard developer commands in a quick-copy format. Every code block is optimized to contain exactly one line for ease of copy-pasting, grouped logically by environment.

---

## 💡 Quick Overview of Docker Compose Files
* **`docker-compose.yml`**: The single, unified configuration file managing all environments (development, production, databases, caches, monitoring, and storage emulation).
* **Docker Compose Profiles**: Controls which services spin up. Profiles are managed inside `.env` using the `COMPOSE_PROFILES` environment variable:
  - `dev`: Daphne development server (with code hot-reloading).
  - `prod`: Gunicorn production workers (no local mounts).
  - `database`: Runs PostgreSQL core database engine.
  - `pgbouncer`: Runs PgBouncer connection pooler.
  - `pgadmin`: Runs pgAdmin management dashboard.
  - `pgexporter`: Runs PostgreSQL database metrics exporter.
  - `caches`: Runs Redis cache container.
  - `redis-insight`: Runs RedisInsight web GUI.
  - `redis-exporter`: Runs Redis caching metrics exporter.
  - `grafana`: Runs Grafana visualization dashboard.
  - `logs-monitoring`: Runs Loki and Promtail collectors.
  - `matrics-monitoring`: Runs Prometheus metrics aggregator.
  - `emulators`: Runs Azurite.
  - `gateway`: Runs Caddy reverse proxy for subdomain/domain routing.
  - `backend-docs`: Runs the Nginx/Docsify developer portal & API/WS docs server on port 8088.

---

## 💻 1. Local Development (Outside Docker on Host Machine)
Use these commands when running Django locally directly on your system using Python/uv.

### Run Django System Check
```bash
uv run python manage.py check
```

### Make Django Migrations
```bash
uv run python manage.py makemigrations
```

### Run Django Migrations
```bash
uv run python manage.py migrate
```

### Create Django Superuser
```bash
uv run python manage.py createsuperuser
```

### Start Local ASGI Server (Daphne)
```bash
uv run daphne -b 127.0.0.1 -p 8000 config.asgi:application
```

### Run Django Unit Tests Locally
```bash
uv run python manage.py test onboard
```

### Seed Database (All Fixtures) Locally
```bash
uv run python manage.py load_data all
```

### Seed Specific Model Fixture Locally
```bash
uv run python manage.py load_data language
```

### Generate Secure Production Credentials & Hashes
```bash
python docker-config/generate_secrets.py
```

---

## 🚢 2. Docker Compose Commands

### Boot Local Development (Backend + Local PostgreSQL + Caching + Monitoring + Emulators + Gateway)
Ensure `COMPOSE_PROFILES=dev,database,pgbouncer,caches,grafana,logs-monitoring,matrics-monitoring,emulators,backend-docs` is set in `.env`:
```bash
docker compose up --build
```

### Boot Local Development in Background (Detached Mode)
```bash
docker compose up -d --build
```

### Boot Daphne ONLY (Bypassing Local DBs / Caches / Monitoring / Gateway)
Change `COMPOSE_PROFILES` to `dev` in `.env` or run explicitly:
```bash
docker compose --profile dev up --build
```

### Boot Production Environment (Split WSGI Gunicorn + ASGI Daphne + Gateway)
Change `COMPOSE_PROFILES` to `prod,database,pgbouncer,caches,grafana,logs-monitoring,matrics-monitoring,gateway` (or omitting local DB profiles if using cloud DBs) and run:
```bash
docker compose up --build
```

### Stop All Active Containers
```bash
docker compose stop
```

### Tear Down Containers & Delete Local Volumes (Wipe Databases)
Stops containers and deletes local pgAdmin, Postgres, and Redis volumes to start fresh:
```bash
docker compose down -v
```

### View Live Backend and DB Logs
```bash
docker compose logs -f
```

---

## 📥 3. Operations Inside Running Docker Containers

> [!NOTE]
> Replace `backend-dev` with `backend-wsgi` (or `backend-asgi` as needed) if you are executing commands inside the production environment container.

### Scan for Model Changes inside Container (Makemigrations)
```bash
docker compose exec backend-dev python manage.py makemigrations
```

### Apply Database Migrations inside Container (Migrate)
```bash
docker compose exec backend-dev python manage.py migrate
```

### Create a Superuser inside Container
```bash
docker compose exec backend-dev python manage.py createsuperuser
```

### Run Django Unit Tests inside Container
```bash
docker compose exec backend-dev python manage.py test onboard
```

### Seed Database (All Fixtures) inside Container
```bash
docker compose exec backend-dev python manage.py load_data all
```

### Seed Specific Model Fixture inside Container
```bash
docker compose exec backend-dev python manage.py load_data language
```


### Access PostgreSQL Shell (psql) inside Database Container
```bash
docker compose exec postgres psql -U postgres -d voicebot_db
```

### Access Redis Command Line (redis-cli) inside Redis Container
```bash
docker compose exec redis redis-cli
```

---

## 🛠️ 4. Container Access & Diagnostics

### Gain Root Access inside the Daphne Container
Enter the backend container as `root` for administrative operations (installs, configs, etc.):
```bash
docker compose exec -u root backend-dev bash
```

### Gain Standard User Access inside the Daphne Container
```bash
docker compose exec backend-dev bash
```

---

## 🗺️ 5. Global Postal Codes Ingestion & Seeding

Use these commands to manage the postal code database setup, initial CSV import, and compressed database seeding.

### Step A: Initial CSV Import (First-time Ingestion)
1. Copy the downloaded CSV file from your Windows host machine to the running Django container:
```bash
docker compose cp "C:\Users\Hello\Downloads\geonames-postal-code.csv" backend-dev:/app/geonames-postal-code.csv
```
2. Execute the import command to parse, validate, and batch-insert the data (supports semicolons separator and column space matching):
```bash
docker compose exec backend-dev python manage.py load_postal_data --csv /app/geonames-postal-code.csv
```

### Step B: Create a Compressed Database Seeding Fixture (For Git/CI/CD)
Once the database has been loaded and validated, run the appropriate command to dump the table's records into a compressed Gzip file:

**On Linux / macOS (or Git Bash / WSL on Windows):**
```bash
docker compose exec postgres bash -c "pg_dump -U postgres -d voicebot_db --table=onboard_postal_code --data-only --inserts | gzip" > onboard/fixtures/postal_codes.sql.gz
```

**On Windows (CMD / PowerShell):**
```powershell
# 1. Run pg_dump and gzip inside the database container (saving to the shared volume directory)
docker exec postgres-voicebot bash -c "pg_dump -U postgres -d voicebot_db --table=onboard_postal_code --data-only --inserts | gzip > /var/lib/postgresql/postal_codes.sql.gz"

# 2. Copy the resulting file from the shared volume to your repository fixtures directory
powershell -Command "Copy-Item .data/postgres/postal_codes.sql.gz onboard/fixtures/postal_codes.sql.gz"
```
*You can now commit and push `onboard/fixtures/postal_codes.sql.gz` to GitHub.*

### Step C: Standard Seeding / CI/CD Setup (Instant Restore)
To seed the database from the compressed fixture in any new environment or during CI/CD pipelines, simply run:
```bash
docker compose exec backend-dev python manage.py load_postal_data
```
> [!NOTE]
> This command automatically checks if data is already present in the table. If records exist, it skips seeding to prevent overrides. To force-truncate the table and reload, add the `--force` flag:
> ```bash
> docker compose exec backend-dev python manage.py load_postal_data --force
> ```

---

## 🌿 6. Git Submodule Updates (Frontend UI)

The Next.js frontend (`qb-voice-ai`) is integrated as a Git submodule. When pulling code on the server, you must use special `git` commands to ensure the frontend code is pulled alongside the backend.

### First-time Setup ONLY
To clone the frontend files into the empty submodule folder:
```bash
git submodule update --init --recursive
```

### Pulling Future Updates
For all future updates, pull both the backend AND frontend code at the exact same time:
```bash
git pull --recurse-submodules
```

### Automate Submodule Pulls (Optional but Recommended)
Tell Git to ALWAYS pull submodules automatically on this server so a regular `git pull` does everything:
```bash
git config --global submodule.recurse true
```

### Rebuilding Only the Frontend Container
After pulling new frontend code, you can build and start **only** the frontend container without restarting the backend services:
```bash
docker compose up -d --build voiceos-frontend
```
